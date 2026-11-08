"""HLS output for the LivePulse ingestor: playlist writing and publishing.

Segments and init segments are handed to `HlsOutput`, which publishes them
in order on a background thread - to a local directory (the Nginx webroot)
or to a GCS / S3 bucket - and republishes the media playlist after each new
segment. Segments that slide out of the playlist window are deleted a little
later, so clients holding an older playlist can still fetch them.
"""

import dataclasses
import logging
import os
import queue
import shutil
import tempfile
import threading
import time
import urllib.parse

log = logging.getLogger("ingestor")

CONTENT_TYPES = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".m4s": "video/iso.segment",
    ".mp4": "video/mp4",
    ".cmfv": "video/mp4",
    ".m4v": "video/mp4",
    ".ts": "video/mp2t",
    ".aac": "audio/aac",
}
PLAYLIST_CACHE = "no-cache, max-age=1"
SEGMENT_CACHE = "public, max-age=86400, immutable"


def content_type(name):
    return CONTENT_TYPES.get(os.path.splitext(name)[1].lower(), "application/octet-stream")


# ---------------------------------------------------------------------------
# Publishers: where files go.
# ---------------------------------------------------------------------------
class LocalPublisher:
    """Writes into a directory, e.g. <nginx webroot>/<stream_id>."""

    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self.description = root

    def put(self, name, src_path, cache_control):
        dst = os.path.join(self.root, name)
        tmp = dst + ".tmp"
        shutil.copyfile(src_path, tmp)
        os.replace(tmp, dst)  # atomic: Nginx never serves a half-written file

    def delete(self, name):
        try:
            os.remove(os.path.join(self.root, name))
        except FileNotFoundError:
            pass


class GcsPublisher:
    """gs://bucket/prefix. Credentials: Application Default Credentials
    (GOOGLE_APPLICATION_CREDENTIALS, workload identity, gcloud auth ...)."""

    def __init__(self, bucket, prefix):
        from google.cloud import storage  # imported lazily: only needed for gs://

        self.bucket = storage.Client().bucket(bucket)
        self.prefix = prefix
        self.description = f"gs://{bucket}/{prefix}"
        try:  # warn early; uploading may still work with object-only permissions
            if not self.bucket.exists():
                log.warning("GCS bucket %s does not exist (or isn't visible to these credentials)", bucket)
        except Exception as e:
            log.warning("Could not check GCS bucket %s: %s", bucket, e)

    def put(self, name, src_path, cache_control):
        blob = self.bucket.blob(self.prefix + name)
        blob.cache_control = cache_control
        blob.upload_from_filename(src_path, content_type=content_type(name))

    def delete(self, name):
        from google.api_core import exceptions

        try:
            self.bucket.blob(self.prefix + name).delete()
        except exceptions.NotFound:
            pass


class S3Publisher:
    """s3://bucket/prefix. Credentials and region from the usual AWS settings
    (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_REGION, instance role ...);
    AWS_ENDPOINT_URL selects an S3-compatible service such as MinIO."""

    def __init__(self, bucket, prefix):
        import boto3  # imported lazily: only needed for s3://

        self.client = boto3.client("s3")
        self.bucket = bucket
        self.prefix = prefix
        self.description = f"s3://{bucket}/{prefix}"
        try:  # warn early; uploading may still work with object-only permissions
            self.client.head_bucket(Bucket=bucket)
        except Exception as e:
            log.warning("S3 bucket %s is not accessible: %s", bucket, e)

    def put(self, name, src_path, cache_control):
        with open(src_path, "rb") as f:
            self.client.put_object(
                Bucket=self.bucket, Key=self.prefix + name, Body=f,
                ContentType=content_type(name), CacheControl=cache_control,
            )

    def delete(self, name):
        self.client.delete_object(Bucket=self.bucket, Key=self.prefix + name)


def make_publisher(cloud_upload_url, nginx_webroot, stream_path):
    """cloud_upload_url (gs:// or s3://) wins; otherwise <webroot>/<stream_path>."""
    if not cloud_upload_url:
        return LocalPublisher(os.path.join(nginx_webroot, stream_path))
    url = urllib.parse.urlparse(cloud_upload_url)
    if not url.netloc:
        raise SystemExit(f"--cloud_upload_url needs a bucket: {cloud_upload_url}")
    prefix = "/".join(p for p in (url.path.strip("/"), stream_path.strip("/")) if p)
    prefix = prefix + "/" if prefix else ""
    if url.scheme == "gs":
        return GcsPublisher(url.netloc, prefix)
    if url.scheme == "s3":
        return S3Publisher(url.netloc, prefix)
    raise SystemExit(f"--cloud_upload_url must start with gs:// or s3://, got {cloud_upload_url}")


# ---------------------------------------------------------------------------
# Playlist
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class Segment:
    name: str
    duration: float
    init: str = None  # init segment (EXT-X-MAP) this segment needs, if any
    discontinuity: bool = False  # EXT-X-DISCONTINUITY before this segment
    seq: int = 0  # media sequence number, assigned by HlsOutput


class MediaPlaylist:
    """A live HLS media playlist with a sliding window (or EVENT if list_size=0)."""

    def __init__(self, list_size):
        self.list_size = list_size
        self.segments = []
        self.next_seq = 0
        self.discontinuity_seq = 0
        self.target = 1
        self.ended = False

    def add(self, seg):
        """Adds a segment; returns the segments that slid out of the window."""
        seg.seq = self.next_seq
        self.next_seq += 1
        self.segments.append(seg)
        # TARGETDURATION must be >= every EXTINF rounded to the nearest integer;
        # it only ever grows, as players expect it to stay constant.
        self.target = max(self.target, int(seg.duration + 0.5))
        removed = []
        while self.list_size and len(self.segments) > self.list_size:
            old = self.segments.pop(0)
            if old.discontinuity:
                self.discontinuity_seq += 1
            removed.append(old)
        return removed

    def render(self):
        uses_map = any(s.init for s in self.segments)
        lines = [
            "#EXTM3U",
            f"#EXT-X-VERSION:{7 if uses_map else 3}",
            f"#EXT-X-TARGETDURATION:{self.target}",
            f"#EXT-X-MEDIA-SEQUENCE:{self.segments[0].seq if self.segments else self.next_seq}",
        ]
        if self.discontinuity_seq:
            lines.append(f"#EXT-X-DISCONTINUITY-SEQUENCE:{self.discontinuity_seq}")
        if not self.list_size:
            lines.append("#EXT-X-PLAYLIST-TYPE:EVENT")
        lines.append("#EXT-X-INDEPENDENT-SEGMENTS")
        current_init = None
        for seg in self.segments:
            if seg.discontinuity:
                lines.append("#EXT-X-DISCONTINUITY")
            if seg.init and seg.init != current_init:
                lines.append(f'#EXT-X-MAP:URI="{seg.init}"')
                current_init = seg.init
            lines.append(f"#EXTINF:{seg.duration:.6f},")
            lines.append(seg.name)
        if self.ended:
            lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Ordered, asynchronous output
# ---------------------------------------------------------------------------
class HlsOutput:
    """Publishes init/media segments and the playlist, in order, off the caller's thread.

    The caller never blocks on uploads, so a slow bucket can't make it miss
    input; the backlog is logged if it builds up.
    """

    RETRIES = 4

    def __init__(self, publisher, playlist_name="index.m3u8", list_size=10, delete_delay=3):
        self.publisher = publisher
        self.playlist_name = playlist_name
        self.playlist = MediaPlaylist(list_size)
        self.delete_delay = delete_delay  # extra segments kept after leaving the window
        self.retired = []  # segments out of the window, not deleted yet
        self.live_inits = {}  # init name -> number of segments (in window or retired) using it
        self.tmpdir = tempfile.mkdtemp(prefix="lp_out_")
        self.tasks = queue.Queue()
        self.worker = threading.Thread(target=self._work, name="hls-output", daemon=True)
        self.worker.start()
        self.published = 0

    # -- called by the ingest side ------------------------------------------
    def add_init(self, name, src_path, move=False):
        """Queues an init segment. With move=True the source file is taken over."""
        self.tasks.put(("init", name, self._own(src_path, move)))

    def add_segment(self, segment, src_path, move=False):
        """Queues a media segment; the playlist is republished once it is uploaded."""
        backlog = self.tasks.qsize()
        if backlog and backlog % 10 == 0:
            log.warning("Output is %d tasks behind (%s is slow?)", backlog, self.publisher.description)
        self.tasks.put(("segment", segment, self._own(src_path, move)))

    def finish(self, endlist=True):
        """Flushes everything; with endlist, marks the playlist as ended."""
        self.tasks.put(("finish", endlist, None))
        self.tasks.join()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _own(self, src_path, move):
        if move:
            return src_path
        fd, tmp = tempfile.mkstemp(dir=self.tmpdir)
        os.close(fd)
        shutil.copyfile(src_path, tmp)
        return tmp

    # -- worker ---------------------------------------------------------------
    def _retry(self, what, fn, *args):
        for attempt in range(self.RETRIES):
            try:
                return fn(*args)
            except Exception as e:  # network / bucket errors: retry, then give up on this file
                if attempt == self.RETRIES - 1:
                    log.error("Giving up on %s after %d attempts: %s", what, self.RETRIES, e)
                    return None
                delay = 2 ** attempt
                log.warning("%s failed (%s); retrying in %ds", what, e, delay)
                time.sleep(delay)

    def _publish_playlist(self):
        path = os.path.join(self.tmpdir, "playlist.m3u8")
        with open(path, "w", newline="\n") as f:
            f.write(self.playlist.render())
        self._retry(f"upload {self.playlist_name}", self.publisher.put, self.playlist_name, path, PLAYLIST_CACHE)

    def _work(self):
        while True:
            kind, item, path = self.tasks.get()
            try:
                if kind == "init":
                    self._retry(f"upload {item}", self.publisher.put, item, path, SEGMENT_CACHE)
                    self.live_inits.setdefault(item, 0)
                elif kind == "segment":
                    self._retry(f"upload {item.name}", self.publisher.put, item.name, path, SEGMENT_CACHE)
                    if item.init:
                        self.live_inits[item.init] = self.live_inits.get(item.init, 0) + 1
                    self.retired += self.playlist.add(item)
                    self._publish_playlist()
                    self.published += 1
                    self._delete_retired()
                elif kind == "finish":
                    self.playlist.ended = item
                    if item and self.playlist.segments:
                        self._publish_playlist()
            finally:
                if path:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                self.tasks.task_done()

    def _delete_retired(self):
        while len(self.retired) > self.delete_delay:
            old = self.retired.pop(0)
            self._retry(f"delete {old.name}", self.publisher.delete, old.name)
            if old.init:
                self.live_inits[old.init] -= 1
                if self.live_inits[old.init] <= 0:
                    del self.live_inits[old.init]
                    self._retry(f"delete {old.init}", self.publisher.delete, old.init)

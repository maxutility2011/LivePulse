"""Follows a live HLS playlist and yields decodable segments.

For fMP4 / CMAF segments the init segment (EXT-X-MAP) is prepended to each
media segment, so every yielded file decodes on its own; TS segments are
yielded as they are. Each segment also gets a wall-clock start time: from
EXT-X-PROGRAM-DATE-TIME when the playlist has it, otherwise estimated from
when the segment appeared (a live segment is listed right after it ends).
"""

import dataclasses
import datetime
import logging
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("processor")
USER_AGENT = "LivePulse-processor/1.0"


class NotYetAvailable(Exception):
    pass


def http_get(url, byterange=None, timeout=15):
    headers = {"User-Agent": USER_AGENT}
    if byterange:
        length, offset = byterange
        headers["Range"] = f"bytes={offset}-{offset + length - 1}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as resp:
            return resp.read(), resp.geturl()
    except urllib.error.HTTPError as e:
        if e.code in (404, 410, 412, 425):
            raise NotYetAvailable(f"HTTP {e.code} for {url}") from e
        raise


@dataclasses.dataclass
class SegmentFile:
    seq: int            # media sequence number in the source playlist
    name: str           # segment file name (last URL path component)
    path: str           # local decodable file (init + segment for fMP4)
    duration: float     # EXTINF
    start: float        # wall-clock start (epoch seconds)
    discontinuity: bool
    playlist_seen_at: float  # when the segment was first listed


def parse_attributes(text):
    return {k: v.strip('"') for k, v in re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', text)}


def parse_byterange(value, last_end):
    length, _, offset = value.partition("@")
    return int(length), int(offset) if offset else last_end


def parse_media_playlist(text, base):
    info = {"target": 6.0, "seq": 0, "endlist": "#EXT-X-ENDLIST" in text, "segments": []}
    duration, disc, init, pdt, last_end, byterange = None, False, None, None, 0, None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-TARGETDURATION:"):
            info["target"] = float(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            info["seq"] = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-KEY:"):
            if parse_attributes(line.split(":", 1)[1]).get("METHOD", "NONE") != "NONE":
                raise SystemExit("Encrypted HLS (EXT-X-KEY) is not supported")
        elif line.startswith("#EXT-X-MAP:"):
            attrs = parse_attributes(line.split(":", 1)[1])
            br = parse_byterange(attrs["BYTERANGE"], 0) if "BYTERANGE" in attrs else None
            init = (urllib.parse.urljoin(base, attrs["URI"]), br)
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            try:
                pdt = datetime.datetime.fromisoformat(line.split(":", 1)[1].replace("Z", "+00:00")).timestamp()
            except ValueError:
                pdt = None
        elif line.startswith("#EXTINF:"):
            duration = float(line[8:].split(",")[0])
        elif line.startswith("#EXT-X-BYTERANGE:"):
            byterange = parse_byterange(line.split(":", 1)[1], last_end)
        elif line.startswith("#EXT-X-DISCONTINUITY") and not line.startswith("#EXT-X-DISCONTINUITY-"):
            disc = True
        elif line and not line.startswith("#") and duration is not None:
            info["segments"].append({
                "seq": info["seq"] + len(info["segments"]), "url": urllib.parse.urljoin(base, line),
                "name": os.path.basename(urllib.parse.urlparse(line).path), "duration": duration,
                "discontinuity": disc, "init": init, "byterange": byterange, "pdt": pdt,
            })
            if byterange:
                last_end = byterange[1] + byterange[0]
            if pdt is not None:
                pdt += duration  # PDT of the next segment unless restated
            duration, disc, byterange = None, False, None
    return info


class HlsReader:
    """Yields SegmentFile objects for new segments of a live (or VOD) HLS playlist."""

    def __init__(self, url, workdir=None, live_edge_segments=3):
        self.url = url
        self.workdir = workdir or tempfile.mkdtemp(prefix="lp_proc_")
        os.makedirs(self.workdir, exist_ok=True)
        self.live_edge_segments = live_edge_segments
        self.stopping = threading.Event()
        self.inits = {}  # (url, byterange) -> bytes
        self.media_url = None
        self.next_start = None  # estimated wall-clock start of the next segment

    def stop(self):
        self.stopping.set()

    def resolve(self):
        text, final_url = http_get(self.url)
        text = text.decode("utf-8", "replace")
        if "#EXT-X-STREAM-INF" not in text:
            return final_url
        variants, attrs = [], None
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("#EXT-X-STREAM-INF:"):
                attrs = parse_attributes(line.split(":", 1)[1])
            elif line and not line.startswith("#") and attrs is not None:
                variants.append((int(attrs.get("BANDWIDTH", 0)), urllib.parse.urljoin(final_url, line)))
                attrs = None
        if len(variants) > 1:
            log.warning("Master playlist has %d variants; using the highest bandwidth one", len(variants))
        return max(variants)[1]

    def fetch(self, url, byterange=None, attempts=4):
        for i in range(attempts):
            try:
                return http_get(url, byterange)[0]
            except (NotYetAvailable, urllib.error.URLError, OSError) as e:
                if i == attempts - 1 or self.stopping.is_set():
                    log.warning("Skipping %s: %s", url, e)
                    return None
                self.stopping.wait(0.5 * (i + 1))

    def segments(self):
        """Generator of SegmentFile; ends when the playlist ends (EXT-X-ENDLIST) or on stop()."""
        while self.media_url is None and not self.stopping.is_set():
            try:
                self.media_url = self.resolve()
            except (NotYetAvailable, urllib.error.URLError, OSError, ValueError) as e:
                log.warning("Can't load %s yet: %s", self.url, e)
                self.stopping.wait(3)
        if self.media_url:
            log.info("Following %s", self.media_url)
        last_seq = None
        while not self.stopping.is_set():
            try:
                text, final_url = http_get(self.media_url)
                info = parse_media_playlist(text.decode("utf-8", "replace"), final_url)
            except (NotYetAvailable, urllib.error.URLError, OSError, ValueError) as e:
                log.warning("Playlist reload failed: %s", e)
                self.stopping.wait(2)
                continue
            seen_at = time.time()
            segs = info["segments"]
            if last_seq is None:
                start = 0 if info["endlist"] else max(0, len(segs) - self.live_edge_segments)
                last_seq = segs[start]["seq"] - 1 if segs else info["seq"] - 1
            new = [s for s in segs if s["seq"] > last_seq]
            gap = bool(new) and new[0]["seq"] > last_seq + 1
            if gap:
                log.warning("Missed segments %d-%d (fell out of the playlist window)", last_seq + 1, new[0]["seq"] - 1)
            # Re-anchor when the duration-chained estimate drifts from "the newest segment just ended"
            # (EXTINF durations don't always add up to wall-clock time).
            drift = (self.next_start is not None and bool(new)
                     and abs(self.next_start + sum(s["duration"] for s in new) - seen_at) > max(2.0, info["target"]))
            if new and (self.next_start is None or gap or new[0]["discontinuity"] or drift):
                # Anchor: the newest listed segment has just ended.
                tail = sum(s["duration"] for s in segs if s["seq"] >= new[0]["seq"])
                self.next_start = seen_at - tail
            for i, s in enumerate(new):
                if self.stopping.is_set():
                    return
                last_seq = s["seq"]
                seg = self.download(s, seen_at, gap and i == 0)
                if seg is not None:
                    yield seg
            if info["endlist"] and (not segs or last_seq >= segs[-1]["seq"]):
                log.info("Playlist ended (EXT-X-ENDLIST)")
                return
            self.stopping.wait(0.5 if new else max(0.5, min(info["target"] / 2, 2.0)))

    def download(self, s, seen_at, gap):
        start = s["pdt"] if s["pdt"] is not None else self.next_start
        self.next_start = start + s["duration"]
        init_bytes = b""
        if s["init"]:
            if s["init"] not in self.inits:
                data = self.fetch(*s["init"])
                if data is None:
                    return None
                self.inits = {s["init"]: data}  # keep only the current init
            init_bytes = self.inits[s["init"]]
        data = self.fetch(s["url"], s["byterange"])
        if data is None:
            return None
        ext = ".mp4" if s["init"] else (os.path.splitext(s["name"])[1] or ".ts")
        path = os.path.join(self.workdir, f"segment_{s['seq']}{ext}")
        with open(path, "wb") as f:
            f.write(init_bytes)
            f.write(data)
        return SegmentFile(s["seq"], s["name"], path, s["duration"], start, s["discontinuity"] or gap, seen_at)

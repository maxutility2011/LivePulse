"""Segmented inputs (HLS / DASH): segments are passed through unchanged.

HLS: the media playlist (or the single variant of a master playlist) is
polled, and every new segment - TS, CMAF or fMP4, plus its EXT-X-MAP init
segment - is copied byte for byte and republished with its original
duration.

DASH: the MPD of the single video Representation is polled (SegmentTemplate
with SegmentTimeline or with @duration), and the init and media segments are
copied byte for byte and published as an HLS playlist (EXT-X-MAP + EXTINF).
This client waits for not-yet-published segments instead of giving up, which
is why it is used rather than FFmpeg's DASH demuxer.
"""

import datetime
import logging
import math
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from hls_output import Segment

log = logging.getLogger("ingestor")
USER_AGENT = "LivePulse-ingestor/1.0"
LIVE_EDGE_SEGMENTS = 3  # start this many segments behind the live edge


class NotYetAvailable(Exception):
    pass


def http_get(url, byterange=None, timeout=15):
    headers = {"User-Agent": USER_AGENT}
    if byterange:
        length, offset = byterange
        headers["Range"] = f"bytes={offset}-{offset + length - 1}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(), resp.geturl()
    except urllib.error.HTTPError as e:
        if e.code in (404, 410, 412, 425):  # typical "not published yet" answers
            raise NotYetAvailable(f"HTTP {e.code} for {url}") from e
        raise


class PullBase:
    def __init__(self, args, url, output):
        self.args = args
        self.url = url
        self.output = output
        self.stopping = threading.Event()
        self.tmpdir = tempfile.mkdtemp(prefix="lp_pull_")
        self.count = 0
        self.inits = {}  # source init URL (+range) -> published name
        self.discontinuity = False

    def save(self, data):
        fd, path = tempfile.mkstemp(dir=self.output.tmpdir)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return path

    def publish_init(self, key, data, ext):
        if key not in self.inits:
            name = f"init_{len(self.inits) + 1:04d}{ext}"
            self.inits[key] = name
            self.output.add_init(name, self.save(data), move=True)
            if len(self.inits) > 1:
                self.discontinuity = True  # a new init means new codec parameters
        return self.inits[key]

    def publish_segment(self, data, duration, ext, init_name, discontinuity=False):
        self.count += 1
        name = f"seg_{self.count:08d}{ext}"
        seg = Segment(name, duration, init=init_name, discontinuity=(discontinuity or self.discontinuity) and self.count > 1)
        self.discontinuity = False
        self.output.add_segment(seg, self.save(data), move=True)
        log.info("%s  %.3fs", name, duration)

    def stop(self):
        self.stopping.set()


def extension(uri, default):
    ext = os.path.splitext(urllib.parse.urlparse(uri).path)[1].lower()
    return ext if ext in (".ts", ".m4s", ".mp4", ".cmfv", ".m4v", ".aac") else default


# ---------------------------------------------------------------------------
# HLS
# ---------------------------------------------------------------------------
def parse_attributes(text):
    return {k: v.strip('"') for k, v in re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', text)}


def parse_byterange(value, last_end):
    length, _, offset = value.partition("@")
    return int(length), int(offset) if offset else last_end


class HlsPull(PullBase):
    def resolve_media_playlist(self):
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
        if not variants:
            raise SystemExit(f"No variants in master playlist {self.url}")
        if len(variants) > 1:
            log.warning("Master playlist has %d variants; using the highest bandwidth one", len(variants))
        if "#EXT-X-MEDIA:" in text and "TYPE=AUDIO" in text:
            log.warning("Alternate audio renditions (EXT-X-MEDIA) are ignored")
        return max(variants)[1]

    def parse_media_playlist(self, text, base):
        info = {"target": 6.0, "seq": 0, "endlist": "#EXT-X-ENDLIST" in text, "segments": []}
        duration, disc, init, last_end, byterange = None, False, None, 0, None
        seq = None
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
            elif line.startswith("#EXTINF:"):
                duration = float(line[8:].split(",")[0])
            elif line.startswith("#EXT-X-BYTERANGE:"):
                byterange = parse_byterange(line.split(":", 1)[1], last_end)
            elif line.startswith("#EXT-X-DISCONTINUITY") and not line.startswith("#EXT-X-DISCONTINUITY-"):
                disc = True
            elif line and not line.startswith("#") and duration is not None:
                seq = info["seq"] + len(info["segments"])
                info["segments"].append({
                    "seq": seq, "url": urllib.parse.urljoin(base, line), "duration": duration,
                    "discontinuity": disc, "init": init, "byterange": byterange,
                })
                if byterange:
                    last_end = byterange[1] + byterange[0]
                duration, disc, byterange = None, False, None
        return info

    def run(self):
        while True:  # the source may not be up yet
            try:
                media_url = self.resolve_media_playlist()
                break
            except (NotYetAvailable, urllib.error.URLError, OSError) as e:
                log.warning("Can't load %s yet: %s", self.url, e)
                if self.stopping.wait(3):
                    return False
        log.info("Following HLS media playlist %s", media_url)
        last_seq = None
        while not self.stopping.is_set():
            try:
                text, final_url = http_get(media_url)
                info = self.parse_media_playlist(text.decode("utf-8", "replace"), final_url)
            except (NotYetAvailable, urllib.error.URLError, OSError, ValueError) as e:
                log.warning("Playlist reload failed: %s", e)
                self.stopping.wait(2)
                continue
            segments = info["segments"]
            if last_seq is None:  # first load: start near the live edge (or at the start for VOD)
                start = 0 if info["endlist"] else max(0, len(segments) - LIVE_EDGE_SEGMENTS)
                last_seq = segments[start]["seq"] - 1 if segments else info["seq"] - 1
            new = [s for s in segments if s["seq"] > last_seq]
            if new and new[0]["seq"] > last_seq + 1:
                log.warning("Missed segments %d-%d (fell out of the source window)", last_seq + 1, new[0]["seq"] - 1)
                new[0]["discontinuity"] = True
            for s in new:
                if self.stopping.is_set():
                    break
                self.copy_segment(s)
                last_seq = s["seq"]
            if info["endlist"] and (not segments or last_seq >= segments[-1]["seq"]):
                log.info("Source playlist ended (EXT-X-ENDLIST)")
                return True
            self.stopping.wait(max(0.5, info["target"] / 2 if not new else 0.5))
        return False

    def copy_segment(self, s):
        init_name = None
        if s["init"]:
            init_url, init_range = s["init"]
            key = (init_url, init_range)
            if key not in self.inits:
                data, _ = self.fetch_retrying(init_url, init_range)
                if data is None:
                    return
                init_name = self.publish_init(key, data, extension(init_url, ".mp4"))
            init_name = self.inits[key]
        data, _ = self.fetch_retrying(s["url"], s["byterange"])
        if data is None:
            self.discontinuity = True
            return
        self.publish_segment(data, s["duration"], extension(s["url"], ".m4s" if s["init"] else ".ts"),
                             init_name, s["discontinuity"])

    def fetch_retrying(self, url, byterange=None, attempts=4):
        for i in range(attempts):
            try:
                return http_get(url, byterange)
            except (NotYetAvailable, urllib.error.URLError, OSError) as e:
                if i == attempts - 1 or self.stopping.is_set():
                    log.warning("Skipping %s: %s", url, e)
                    return None, None
                self.stopping.wait(0.5 * (i + 1))


# ---------------------------------------------------------------------------
# DASH
# ---------------------------------------------------------------------------
def iso_duration(text, default=None):
    """PT1H2M3.5S -> seconds."""
    if not text:
        return default
    m = re.match(r"P(?:(\d+(?:\.\d+)?)D)?(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?$", text)
    if not m:
        return default
    d, h, mi, s = (float(x) if x else 0.0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def iso_datetime(text):
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    return datetime.datetime.fromisoformat(text).timestamp()


def strip_ns(root):
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def fill_template(template, rep_id, bandwidth, number=None, time_=None):
    def sub(m):
        name, fmt = m.group(1), m.group(2)
        value = {"RepresentationID": rep_id, "Bandwidth": bandwidth, "Number": number, "Time": time_}.get(name)
        if value is None:
            return m.group(0)
        return (fmt % value) if fmt else str(value)

    return re.sub(r"\$(RepresentationID|Bandwidth|Number|Time)(%0\d+d)?\$", sub, template).replace("$$", "$")


class DashPull(PullBase):
    def load(self):
        data, final_url = http_get(self.url)
        root = strip_ns(ET.fromstring(data))
        mpd = {
            "dynamic": root.get("type", "static") == "dynamic",
            "ast": iso_datetime(root.get("availabilityStartTime")),
            "update": iso_duration(root.get("minimumUpdatePeriod"), 2.0),
            "tsb": iso_duration(root.get("timeShiftBufferDepth"), None),
        }
        base = final_url
        for el in root.findall("BaseURL")[:1]:
            base = urllib.parse.urljoin(base, el.text.strip())
        periods = root.findall("Period")
        if not periods:
            raise SystemExit("MPD has no Period")
        period = periods[-1]  # the current period of a live stream
        if len(periods) > 1:
            log.debug("MPD has %d periods; using the last", len(periods))
        mpd["period_id"] = period.get("id", str(len(periods)))
        mpd["period_start"] = iso_duration(period.get("start"), 0.0)
        for el in period.findall("BaseURL")[:1]:
            base = urllib.parse.urljoin(base, el.text.strip())

        def is_video(el):
            return (el.get("contentType") == "video" or (el.get("mimeType") or "").startswith("video")
                    or any((r.get("mimeType") or "").startswith("video") for r in el.findall("Representation")))

        sets = [a for a in period.findall("AdaptationSet") if is_video(a)]
        if not sets:
            raise SystemExit("MPD has no video AdaptationSet")
        if len(period.findall("AdaptationSet")) > len(sets[:1]):
            log.debug("Only the video AdaptationSet is ingested")
        aset = sets[0]
        for el in aset.findall("BaseURL")[:1]:
            base = urllib.parse.urljoin(base, el.text.strip())
        reps = aset.findall("Representation")
        if not reps:
            raise SystemExit("Video AdaptationSet has no Representation")
        if len(reps) > 1:
            log.warning("AdaptationSet has %d Representations; using the highest bandwidth one", len(reps))
        rep = max(reps, key=lambda r: int(r.get("bandwidth", 0)))
        for el in rep.findall("BaseURL")[:1]:
            base = urllib.parse.urljoin(base, el.text.strip())
        tmpl = rep.find("SegmentTemplate")
        if tmpl is None:
            tmpl = aset.find("SegmentTemplate")
        if tmpl is None:
            raise SystemExit("Only DASH SegmentTemplate is supported (no SegmentList / SegmentBase)")
        outer = aset.find("SegmentTemplate")
        get = lambda k, d=None: tmpl.get(k) or (outer.get(k) if outer is not None else None) or d  # noqa: E731
        mpd.update({
            "base": base, "rep_id": rep.get("id", ""), "bandwidth": rep.get("bandwidth", "0"),
            "timescale": int(get("timescale", "1")), "start_number": int(get("startNumber", "1")),
            "pto": int(get("presentationTimeOffset", "0")),
            "media": get("media"), "init": get("initialization"), "duration": get("duration"),
        })
        timeline = tmpl.find("SegmentTimeline")
        if timeline is None and outer is not None:
            timeline = outer.find("SegmentTimeline")
        mpd["timeline"] = None
        if timeline is not None:
            entries, t, number = [], 0, mpd["start_number"]
            s_elements = timeline.findall("S")
            for i, s in enumerate(s_elements):
                if s.get("t") is not None:
                    t = int(s.get("t"))
                d, r = int(s.get("d")), int(s.get("r", "0"))
                if r < 0:  # repeat until the next S, or (last S) up to now
                    nxt = s_elements[i + 1].get("t") if i + 1 < len(s_elements) else None
                    if nxt:
                        end = int(nxt)
                    elif mpd["dynamic"] and mpd["ast"]:
                        now = time.time() - mpd["ast"] - mpd["period_start"]
                        end = mpd["pto"] + int(now * mpd["timescale"]) - d  # complete segments only
                    else:
                        end = t + d
                    r = max(0, math.ceil((end - t) / d) - 1)
                for _ in range(r + 1):
                    entries.append((number, t, d))
                    t += d
                    number += 1
            mpd["timeline"] = entries
        if mpd["media"] is None:
            raise SystemExit("SegmentTemplate has no @media")
        return mpd

    def available(self, mpd):
        """Returns [(number, time, duration)] of segments that should exist now."""
        if mpd["timeline"] is not None:
            return mpd["timeline"]
        if not mpd["duration"]:
            raise SystemExit("SegmentTemplate needs a SegmentTimeline or @duration")
        dur = int(mpd["duration"])
        seg_s = dur / mpd["timescale"]
        if not mpd["dynamic"]:
            raise SystemExit("Static (VOD) DASH with @duration templates is not supported; use a live MPD")
        elapsed = time.time() - (mpd["ast"] or 0) - mpd["period_start"]
        latest = mpd["start_number"] + int(elapsed // seg_s) - 1  # the newest *complete* segment
        earliest = latest - int((mpd["tsb"] or seg_s * 10) // seg_s) + 1
        first = max(mpd["start_number"], earliest)
        return [(n, mpd["pto"] + (n - mpd["start_number"]) * dur, dur) for n in range(first, latest + 1)]

    def run(self):
        log.info("Following DASH manifest %s", self.url)
        done = None  # newest segment number published
        period = None
        while not self.stopping.is_set():
            try:
                mpd = self.load()
                segs = self.available(mpd)
            except (NotYetAvailable, urllib.error.URLError, OSError, ET.ParseError, ValueError) as e:
                log.warning("MPD reload failed: %s", e)
                self.stopping.wait(2)
                continue
            if period != mpd["period_id"]:
                if period is not None:
                    log.info("New period %s", mpd["period_id"])
                    self.discontinuity = True
                    done = None
                period = mpd["period_id"]
            init_name = None
            if mpd["init"]:
                init_url = urllib.parse.urljoin(mpd["base"], fill_template(mpd["init"], mpd["rep_id"], mpd["bandwidth"]))
                if init_url not in self.inits:
                    data = self.fetch_until(init_url, 10)
                    if data is None:
                        continue
                    self.publish_init(init_url, data, extension(init_url, ".mp4"))
                init_name = self.inits[init_url]
            if done is None:
                start = 0 if not mpd["dynamic"] else max(0, len(segs) - LIVE_EDGE_SEGMENTS)
                done = segs[start][0] - 1 if segs else mpd["start_number"] - 1
            new = [s for s in segs if s[0] > done]
            if new and new[0][0] > done + 1:
                log.warning("Missed segments %d-%d", done + 1, new[0][0] - 1)
                self.discontinuity = True
            for number, t, d in new:
                if self.stopping.is_set():
                    break
                url = urllib.parse.urljoin(mpd["base"], fill_template(mpd["media"], mpd["rep_id"], mpd["bandwidth"], number, t))
                data = self.fetch_until(url, max(4.0, 2 * d / mpd["timescale"]))
                if data is None:
                    self.discontinuity = True
                else:
                    self.publish_segment(data, d / mpd["timescale"], extension(url, ".m4s"), init_name)
                done = number
            if not mpd["dynamic"] and (not segs or done >= segs[-1][0]):
                log.info("Static MPD fully ingested")
                return True
            # Poll at half a segment: minimumUpdatePeriod alone can be huge (PT500S
            # from FFmpeg) for @duration templates, where new segments appear by clock.
            seg_seconds = segs[-1][2] / mpd["timescale"] if segs else 2.0
            self.stopping.wait(max(0.5, min(mpd["update"], seg_seconds) / 2))
        return False

    def fetch_until(self, url, patience):
        """Fetches url, waiting up to `patience` seconds for it to be published."""
        deadline = time.time() + patience
        while not self.stopping.is_set():
            try:
                return http_get(url)[0]
            except (NotYetAvailable, urllib.error.URLError, OSError) as e:
                if time.time() >= deadline:
                    log.warning("Skipping %s: %s", url, e)
                    return None
                self.stopping.wait(0.5)
        return None

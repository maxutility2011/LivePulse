"""Real-time web UI for the processor: score store, JSON API and Server-Sent Events.

Endpoints:
  GET /                 the chart page (static/index.html)
  GET /api/state        status + recent frames/segments (?window=seconds, default 600)
  GET /events           Server-Sent Events: every scored batch is pushed as soon as it exists
                        (?after=<frame id>&seg_after=<segment id>, or Last-Event-ID to resume)
  GET /healthz          liveness
"""

import collections
import http.server
import json
import logging
import os
import threading
import time
import urllib.parse

log = logging.getLogger("processor")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


class ScoreStore:
    """In-memory ring of recent frame scores and segment summaries, with change notification."""

    def __init__(self, max_frames=200_000, max_segments=20_000):
        self.cond = threading.Condition()
        self.frames = collections.deque(maxlen=max_frames)  # [id, t, score, seq]
        self.segments = collections.deque(maxlen=max_segments)  # dicts with "id"
        self.next_frame_id = 1
        self.next_segment_id = 1
        self.status = {}
        self.status_version = 0

    def add_frames(self, times, scores, seq):
        with self.cond:
            for t, s in zip(times, scores):
                self.frames.append([self.next_frame_id, round(float(t), 3), round(float(s), 4), seq])
                self.next_frame_id += 1
            self.cond.notify_all()

    def add_segment(self, summary):
        with self.cond:
            summary = dict(summary, id=self.next_segment_id)
            self.segments.append(summary)
            self.next_segment_id += 1
            self.cond.notify_all()

    def update_status(self, **fields):
        with self.cond:
            self.status.update(fields)
            self.status_version += 1
            self.cond.notify_all()

    def snapshot(self, window):
        with self.cond:
            cutoff = (self.frames[-1][1] - window) if self.frames else 0
            frames = [f for f in self.frames if f[1] >= cutoff]
            segments = [s for s in self.segments if s["end"] >= cutoff]
            return {"status": dict(self.status), "frames": frames, "segments": segments,
                    "last_frame_id": self.next_frame_id - 1, "last_segment_id": self.next_segment_id - 1}

    def wait_for_update(self, frame_after, seg_after, status_seen, timeout):
        """Blocks until something newer exists (or timeout); returns the delta."""
        with self.cond:
            self.cond.wait_for(lambda: (self.next_frame_id - 1 > frame_after or self.next_segment_id - 1 > seg_after
                                        or self.status_version != status_seen), timeout)
            frames = [f for f in self.frames if f[0] > frame_after] if self.next_frame_id - 1 > frame_after else []
            segments = [s for s in self.segments if s["id"] > seg_after] if self.next_segment_id - 1 > seg_after else []
            status = dict(self.status) if self.status_version != status_seen else None
            return frames, segments, status, self.status_version


def make_server(store, host, port):
    with open(os.path.join(STATIC, "index.html"), "rb") as f:
        page = f.read()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def send_body(self, body, ctype, status=200):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(url.query)
            if url.path in ("/", "/index.html"):
                self.send_body(page, "text/html; charset=utf-8")
            elif url.path == "/healthz":
                self.send_body(b"ok\n", "text/plain")
            elif url.path == "/api/state":
                window = float(q.get("window", ["600"])[0])
                self.send_body(json.dumps(store.snapshot(window)).encode(), "application/json")
            elif url.path == "/events":
                self.events(q)
            else:
                self.send_body(b"not found\n", "text/plain", 404)

        def events(self, q):
            last = self.headers.get("Last-Event-ID", "")
            frame_after = int(q.get("after", ["0"])[0])
            seg_after = int(q.get("seg_after", ["0"])[0])
            if ":" in last:  # resume after a dropped connection: "<frame id>:<segment id>"
                frame_after, seg_after = (int(x) for x in last.split(":", 1))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("X-Accel-Buffering", "no")  # don't let a proxy buffer the stream
            self.end_headers()
            status_seen = -1
            try:
                while True:
                    frames, segments, status, status_seen = store.wait_for_update(
                        frame_after, seg_after, status_seen, timeout=15)
                    if frames:
                        frame_after = frames[-1][0]
                    if segments:
                        seg_after = segments[-1]["id"]
                    if frames or segments or status:
                        payload = json.dumps({"frames": frames, "segments": segments, "status": status})
                        msg = f"id: {frame_after}:{seg_after}\nevent: update\ndata: {payload}\n\n"
                    else:
                        msg = ": keep-alive\n\n"
                    self.wfile.write(msg.encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                return

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def serve(store, host, port):
    server = make_server(store, host, port)
    threading.Thread(target=server.serve_forever, name="webui", daemon=True).start()
    log.info("Web UI on http://%s:%d/", "localhost" if host in ("0.0.0.0", "") else host, port)
    return server

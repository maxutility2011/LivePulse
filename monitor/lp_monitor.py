"""LivePulse monitor: tracks, aggregates and exports quality metrics.

Follows one or more scores files written by processor/lp_processor.py and
keeps per-stream state: the last UVQ score, rolling mean/min/max over
--window, segment counts by status, a low-quality alert (score below
--low_score for --alert_after segments in a row) and a stale alert (no new
segment for --stale_after). It prints a report every --report_interval and
can export everything:

  --prometheus_port  serves /metrics for Prometheus (and Grafana on top of it)
  --statsd           sends DogStatsD metrics to a Datadog agent (UDP)
  --summary_json     rewrites a JSON summary file

Examples:
    python monitor/lp_monitor.py live/cam1/scores.jsonl
    python monitor/lp_monitor.py live/*/scores.jsonl --prometheus_port 9108 --statsd 127.0.0.1:8125
"""

import argparse
import collections
import datetime
import glob
import http.server
import json
import os
import socket
import threading
import time


def parse_duration(text):
    """'90' / '90s' / '30m' / '2h' / '7d' -> seconds."""
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    text = text.strip().lower()
    try:
        seconds = float(text[:-1]) * units[text[-1]] if text and text[-1] in units else float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid duration {text!r} (use e.g. 90s, 30m, 2h)")
    if seconds <= 0:
        raise argparse.ArgumentTypeError("duration must be positive")
    return seconds


def parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n\n".join(__doc__.split("\n\n")[1:]),
    )
    parser.add_argument("scores", nargs="+", help="Scores file(s) from lp_processor.py; wildcards are expanded")
    parser.add_argument("--window", type=parse_duration, default=300.0, help="Rolling window (default 5m)")
    parser.add_argument("--low_score", type=float, default=3.0, help="UVQ score counted as low quality (default 3.0)")
    parser.add_argument("--alert_after", type=int, default=3, help="Low segments in a row before alerting (default 3)")
    parser.add_argument(
        "--stale_after", type=parse_duration, default=60.0,
        help="Alert when a stream has no new scored segment for this long (default 60s)",
    )
    parser.add_argument("--report_interval", type=parse_duration, default=60.0, help="Console report period (default 60s)")
    parser.add_argument("--summary_json", default="", help="Rewrite this JSON summary file every few seconds")
    parser.add_argument("--prometheus_port", type=int, default=0, help="Serve Prometheus metrics on this port")
    parser.add_argument("--prometheus_host", default="0.0.0.0", help="Bind address for --prometheus_port")
    parser.add_argument("--statsd", default="", help="DogStatsD host:port, e.g. 127.0.0.1:8125 (Datadog agent)")
    parser.add_argument("--statsd_prefix", default="livepulse", help="DogStatsD metric prefix (default livepulse)")
    parser.add_argument("--tags", default="", help="Extra DogStatsD tags, e.g. env:prod,site:nyc")
    parser.add_argument("--poll_interval", type=float, default=1.0, help="Seconds between scores-file checks")
    return parser.parse_args()


def log(message):
    print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def to_epoch(iso_text, default):
    try:
        return datetime.datetime.fromisoformat(iso_text).timestamp()
    except (TypeError, ValueError):
        return default


class JsonlTail:
    """Yields new complete JSON lines appended to a file, across polls."""

    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.partial = b""

    def read_new(self):
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                data = f.read()
        except FileNotFoundError:
            return []
        self.offset += len(data)
        lines = (self.partial + data).split(b"\n")
        self.partial = lines.pop()
        records = []
        for line in lines:
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return records


class StreamState:
    def __init__(self, name):
        self.name = name
        self.window = collections.deque()  # (finished_at epoch, score)
        self.last_score = None
        self.last_segment = None
        self.last_finished = None  # epoch of the newest segment's finish time
        self.last_scoring_seconds = None
        self.counts = collections.Counter()
        self.low_streak = 0
        self.low_alert = False
        self.stale = False

    def prune(self, now, window):
        while self.window and self.window[0][0] < now - window:
            self.window.popleft()

    def stats(self):
        scores = [s for _, s in self.window]
        if not scores:
            return {"count": 0, "mean": None, "min": None, "max": None}
        return {"count": len(scores), "mean": sum(scores) / len(scores), "min": min(scores), "max": max(scores)}


class StatsD:
    """Minimal DogStatsD client (UDP, fire-and-forget)."""

    def __init__(self, address, prefix, tags):
        host, port = address.rsplit(":", 1)
        self.addr = (host, int(port))
        self.prefix = prefix
        self.tags = [t for t in tags.split(",") if t]
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(self, name, value, kind, tags=()):
        all_tags = self.tags + list(tags)
        line = f"{self.prefix}.{name}:{value}|{kind}" + (f"|#{','.join(all_tags)}" if all_tags else "")
        try:
            self.sock.sendto(line.encode(), self.addr)
        except OSError:
            pass


class Monitor:
    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()
        self.streams = {}
        self.tails = {}
        self.statsd = StatsD(args.statsd, args.statsd_prefix, args.tags) if args.statsd else None

    def discover(self):
        for pattern in self.args.scores:
            for path in glob.glob(pattern) or [pattern]:
                if path not in self.tails:
                    self.tails[path] = JsonlTail(path)

    def state(self, name):
        if name not in self.streams:
            self.streams[name] = StreamState(name)
        return self.streams[name]

    def ingest(self, record, live):
        """Updates state from one scores record; `live` = not part of the startup catch-up."""
        st = self.state(record.get("stream") or "default")
        status = record.get("status", "ok")
        st.counts[status] += 1
        finished = to_epoch(record.get("finished_at"), time.time())
        st.last_finished = max(st.last_finished or finished, finished)
        tags = [f"stream:{st.name}"]
        if self.statsd and live:
            self.statsd.send("segments", 1, "c", tags + [f"status:{status}"])
        if status != "ok" or record.get("uvq1p5_score") is None:
            return
        score = float(record["uvq1p5_score"])
        st.last_score, st.last_segment = score, record.get("segment")
        st.last_scoring_seconds = record.get("scoring_seconds")
        st.window.append((finished, score))
        if self.statsd and live:
            self.statsd.send("uvq.score", round(score, 4), "g", tags)
        if score < self.args.low_score:
            st.low_streak += 1
            if not st.low_alert and st.low_streak >= self.args.alert_after:
                st.low_alert = True
                if live:
                    log(f"ALERT {st.name}: UVQ below {self.args.low_score:g} for {st.low_streak} segments "
                        f"(last {score:.3f}, {st.last_segment})")
        else:
            if st.low_alert and live:
                log(f"RECOVERED {st.name}: UVQ {score:.3f} after {st.low_streak} low segments")
            st.low_alert, st.low_streak = False, 0

    def poll(self, live=True):
        self.discover()
        with self.lock:
            for tail in self.tails.values():
                for record in tail.read_new():
                    self.ingest(record, live)
            now = time.time()
            for st in self.streams.values():
                st.prune(now, self.args.window)
                stale = st.last_finished is not None and now - st.last_finished > self.args.stale_after
                if stale != st.stale and live:
                    log(f"STALE {st.name}: no new segment for {now - st.last_finished:.0f}s" if stale
                        else f"RESUMED {st.name}: segments are arriving again")
                st.stale = stale

    def summary(self):
        now = time.time()
        out = {"generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
               "window_seconds": self.args.window, "low_score": self.args.low_score, "streams": {}}
        with self.lock:
            for st in self.streams.values():
                stats = st.stats()
                out["streams"][st.name] = {
                    "last_score": st.last_score,
                    "last_segment": st.last_segment,
                    "seconds_since_last_segment": None if st.last_finished is None else round(now - st.last_finished, 1),
                    "window": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in stats.items()},
                    "segments": dict(st.counts),
                    "low_quality_alert": st.low_alert,
                    "stale": st.stale,
                }
        return out

    def report(self):
        s = self.summary()
        if not s["streams"]:
            log("No scores yet")
        for name, st in s["streams"].items():
            w = st["window"]
            flags = (" [LOW QUALITY]" if st["low_quality_alert"] else "") + (" [STALE]" if st["stale"] else "")
            window = (f"{self.args.window / 60:g}m mean {w['mean']:.3f} min {w['min']:.3f} max {w['max']:.3f} "
                      f"({w['count']} seg)") if w["count"] else "no scores in window"
            last = f"{st['last_score']:.3f}" if st["last_score"] is not None else "-"
            counts = " ".join(f"{k} {v}" for k, v in sorted(st["segments"].items()))
            log(f"{name}: last {last}  {window}  [{counts}]{flags}")

    def export_gauges(self):
        if not self.statsd:
            return
        for name, st in self.summary()["streams"].items():
            tags = [f"stream:{name}"]
            for key in ("mean", "min", "max"):
                if st["window"][key] is not None:
                    self.statsd.send(f"uvq.window.{key}", st["window"][key], "g", tags)
            self.statsd.send("alert.low_quality", int(st["low_quality_alert"]), "g", tags)
            self.statsd.send("alert.stale", int(st["stale"]), "g", tags)

    def write_summary(self):
        if not self.args.summary_json:
            return
        tmp = self.args.summary_json + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.summary(), f, indent=2)
        os.replace(tmp, self.args.summary_json)

    def prometheus_text(self):
        lines = []

        def metric(name, kind, help_text, samples):
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {kind}")
            for labels, value in samples:
                label_text = ",".join(f'{k}="{v}"' for k, v in labels.items())
                lines.append(f"{name}{{{label_text}}} {value}")

        s = self.summary()
        streams = s["streams"].items()
        win = f"{s['window_seconds']:g}s"
        metric("livepulse_uvq_score", "gauge", "UVQ 1.5 score of the newest segment",
               [({"stream": n}, st["last_score"]) for n, st in streams if st["last_score"] is not None])
        for key in ("mean", "min", "max"):
            metric(f"livepulse_uvq_score_window_{key}", "gauge", f"UVQ 1.5 score {key} over the rolling window",
                   [({"stream": n, "window": win}, st["window"][key]) for n, st in streams
                    if st["window"][key] is not None])
        metric("livepulse_segments_total", "counter", "Segments seen, by processing status",
               [({"stream": n, "status": k}, v) for n, st in streams for k, v in st["segments"].items()])
        metric("livepulse_seconds_since_last_segment", "gauge", "Age of the newest segment",
               [({"stream": n}, st["seconds_since_last_segment"]) for n, st in streams
                if st["seconds_since_last_segment"] is not None])
        metric("livepulse_low_quality_alert", "gauge", "1 while UVQ stays below --low_score",
               [({"stream": n}, int(st["low_quality_alert"])) for n, st in streams])
        metric("livepulse_stale_alert", "gauge", "1 while no new segments arrive",
               [({"stream": n}, int(st["stale"])) for n, st in streams])
        return "\n".join(lines) + "\n"

    def serve_prometheus(self):
        monitor = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.split("?")[0] not in ("/metrics", "/"):
                    self.send_error(404)
                    return
                body = monitor.prometheus_text().encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer((self.args.prometheus_host, self.args.prometheus_port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        log(f"Prometheus metrics on http://{self.args.prometheus_host}:{self.args.prometheus_port}/metrics")

    def run(self):
        self.poll(live=False)  # catch up on existing history without alert spam
        log(f"Following {', '.join(self.tails) or self.args.scores}")
        if self.args.prometheus_port:
            self.serve_prometheus()
        if self.statsd:
            log(f"Sending DogStatsD metrics to {self.args.statsd}")
        self.report()
        next_report = time.time() + self.args.report_interval
        next_export = 0.0
        while True:
            time.sleep(self.args.poll_interval)
            self.poll()
            now = time.time()
            if now >= next_export:
                self.export_gauges()
                self.write_summary()
                next_export = now + 10
            if now >= next_report:
                self.report()
                next_report = now + self.args.report_interval


def main():
    monitor = Monitor(parse_args())
    try:
        monitor.run()
    except KeyboardInterrupt:
        monitor.report()


if __name__ == "__main__":
    main()

"""LivePulse processor: UVQ 1.5 quality scores for a live HLS stream, with a real-time web UI.

Takes the URL of an HLS variant playlist (e.g. the ingestor's output), downloads
segment after segment, prepends the init segment to every fMP4 media segment
so it decodes on its own, and scores its frames with Google's UVQ 1.5 model -
on the GPU with TensorRT when one is available, otherwise on the CPU with ONNX
Runtime. UVQ samples --fps frames per second: 5 on the GPU, 1 on the CPU by
default. Scores are pushed to the web UI (--http_port) batch by batch as they
are computed, and optionally appended per segment to a JSON-lines file
(--output) for the monitor.

Examples:
  python processor/lp_processor.py --input_url http://ingestor/cam1/index.m3u8
  python processor/lp_processor.py --input_url https://storage.googleapis.com/bucket/live/cam1/index.m3u8 \\
      --device cpu --output /data/cam1_scores.jsonl
"""

import argparse
import datetime
import json
import logging
import os
import queue
import shutil
import signal
import statistics
import sys
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from hls_reader import HlsReader  # noqa: E402
from scorer import Scorer  # noqa: E402
from webui import ScoreStore, serve  # noqa: E402

log = logging.getLogger("processor")


def first_existing(*paths):
    for p in paths:
        if p and os.path.exists(p):
            return p
    return paths[-1]


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n\n".join(__doc__.split("\n\n")[1:]),
    )
    parser.add_argument("--input_url", required=True, help="HLS variant (media) playlist URL; a master playlist works too")
    parser.add_argument("--stream_id", default=None, help="Stream name in the UI and output (default: from the URL)")
    dev = parser.add_argument_group("scoring")
    dev.add_argument("--device", choices=["auto", "gpu", "cpu"], default="auto",
                     help="auto = GPU (TensorRT) when available, else CPU (ONNX Runtime)")
    dev.add_argument("--fps", type=int, default=None,
                     help="Frames UVQ samples per second of video (default 5 on GPU, 1 on CPU); "
                          "e.g. 5 = every 5th frame of a 25 fps stream")
    dev.add_argument("--onnx", default=first_existing(os.environ.get("LP_ONNX"), "/opt/livepulse/models/uvq1p5.onnx",
                                                      os.path.join(HERE, "uvq_trt", "uvq1p5.onnx")),
                     help="UVQ 1.5 ONNX model (from uvq_trt/export_onnx.py)")
    dev.add_argument("--engine", default=None, help="TensorRT engine to use instead of building/caching one")
    dev.add_argument("--engine_cache_dir",
                     default=os.environ.get("LP_ENGINE_CACHE") or ("/var/cache/livepulse" if os.path.isdir("/opt/livepulse")
                                                                   else os.path.join(HERE, "uvq_trt", "engines")),
                     help="Where GPU-specific TensorRT engines are built and cached")
    dev.add_argument("--threads", type=int, default=None, help="CPU threads for ONNX Runtime (default: all cores)")
    dev.add_argument("--batch_size", type=int, default=None, help="Frames per inference batch (default 8 GPU / 1 CPU)")
    dev.add_argument("--transpose", choices=["auto", "on", "off"], default="auto",
                     help="Transpose frames before scoring; auto = for portrait video")
    flow = parser.add_argument_group("stream handling")
    flow.add_argument("--max_backlog", type=int, default=3,
                      help="If more than N segments wait for scoring, skip the oldest to stay live (0 = never skip)")
    flow.add_argument("--live_edge_segments", type=int, default=3, help="Start this many segments behind the live edge")
    flow.add_argument("--max_segments", type=int, default=0, help="Stop after scoring N segments (0 = run forever)")
    out = parser.add_argument_group("output")
    out.add_argument("--http_host", default="0.0.0.0", help="Web UI bind address")
    out.add_argument("--http_port", type=int, default=8000, help="Web UI port (0 = no web UI; default 8000)")
    out.add_argument("--low_score", type=float, default=3.0, help="Threshold line drawn in the UI (default 3.0)")
    out.add_argument("--output", default="", help="Append one JSON line per scored segment (monitor input)")
    out.add_argument("--history_frames", type=int, default=200_000, help="Frame scores kept in memory for the UI")
    parser.add_argument("--ffmpeg_path", default="ffmpeg")
    parser.add_argument("--log_level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)
    if args.stream_id is None:
        parts = [p for p in urllib.parse.urlparse(args.input_url).path.split("/") if p]
        args.stream_id = parts[-2] if len(parts) >= 2 else (parts[-1] if parts else "stream")
    return args


def iso(t):
    return datetime.datetime.fromtimestamp(t).isoformat(timespec="milliseconds")


class Processor:
    def __init__(self, args, scorer, store):
        self.args = args
        self.scorer = scorer
        self.store = store
        self.reader = HlsReader(args.input_url, live_edge_segments=args.live_edge_segments)
        self.pending = queue.Queue()
        self.counts = {"ok": 0, "skipped": 0, "error": 0}
        self.stopping = threading.Event()

    def read_loop(self):
        try:
            for seg in self.reader.segments():
                self.pending.put(seg)
        finally:
            self.pending.put(None)

    def write_record(self, record):
        if self.args.output:
            with open(self.args.output, "a") as f:
                f.write(json.dumps(record) + "\n")

    def record(self, seg, status, **fields):
        return {"stream": self.args.stream_id, "segment": seg.name, "seq": seg.seq, "status": status,
                "start": iso(seg.start), "finished_at": iso(seg.start + seg.duration), "duration": seg.duration,
                "scored_at": iso(time.time()), "model": "uvq1p5", "device": self.scorer.device,
                "backend": self.scorer.backend, "fps": self.scorer.fps, **fields}

    def publish_status(self, **extra):
        self.store.update_status(stream_id=self.args.stream_id, input_url=self.args.input_url,
                                 device=self.scorer.device, backend=self.scorer.backend, fps=self.scorer.fps,
                                 low_score=self.args.low_score, counts=dict(self.counts),
                                 backlog=self.pending.qsize(), **extra)

    def score_segment(self, seg):
        started = time.time()
        times, scores = [], []
        try:
            for first, batch in self.scorer.score(seg.path):
                t = [seg.start + (first + i) / self.scorer.fps for i in range(len(batch))]
                self.store.add_frames(t, batch, seg.seq)  # live: pushed to the UI right away
                times += t
                scores += batch.tolist()
        except Exception as e:
            log.warning("%s: scoring failed: %s", seg.name, e)
            self.counts["error"] += 1
            self.write_record(self.record(seg, "error", error=str(e)))
            return
        finally:
            try:
                os.remove(seg.path)
            except OSError:
                pass
        elapsed = time.time() - started
        if not scores:
            self.counts["error"] += 1
            self.write_record(self.record(seg, "error", error="no frames decoded"))
            log.warning("%s: no frames decoded", seg.name)
            return
        self.counts["ok"] += 1
        mean = statistics.fmean(scores)
        latency = time.time() - seg.playlist_seen_at  # from appearing in the playlist to scored
        self.store.add_segment({"seq": seg.seq, "name": seg.name, "start": round(seg.start, 3),
                                "end": round(seg.start + seg.duration, 3), "mean": round(mean, 4),
                                "min": round(min(scores), 4), "frames": len(scores),
                                "discontinuity": seg.discontinuity})
        self.write_record(self.record(seg, "ok", uvq1p5_score=mean, per_frame_scores=scores,
                                      frame_times=[iso(t) for t in times], scoring_seconds=round(elapsed, 3),
                                      delay_seconds=round(latency, 3)))
        self.publish_status(last_segment=seg.name, last_score=round(mean, 4), last_latency=round(latency, 2),
                            last_scoring_seconds=round(elapsed, 3))
        slow = "  (slower than real time)" if elapsed > seg.duration else ""
        log.info("%s  UVQ %.4f  [%d frames in %.2fs, scored %.1fs after it was published, backlog %d]%s",
                 seg.name, mean, len(scores), elapsed, latency, self.pending.qsize(), slow)

    def run(self):
        reader = threading.Thread(target=self.read_loop, name="hls-reader", daemon=True)
        reader.start()
        self.publish_status(started_at=iso(time.time()))
        while not self.stopping.is_set():
            try:
                seg = self.pending.get(timeout=0.5)
            except queue.Empty:
                continue
            if seg is None:
                log.info("Input ended")
                break
            while self.args.max_backlog and self.pending.qsize() > self.args.max_backlog:
                log.warning("Behind by %d segments: skipping %s", self.pending.qsize() + 1, seg.name)
                self.counts["skipped"] += 1
                self.write_record(self.record(seg, "skipped"))
                os.remove(seg.path)
                seg = self.pending.get()
                if seg is None:
                    return
            self.score_segment(seg)
            if self.args.max_segments and self.counts["ok"] >= self.args.max_segments:
                break

    def stop(self):
        self.stopping.set()
        self.reader.stop()


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S", stream=sys.stdout)
    if not os.path.exists(args.onnx) and not args.engine:
        raise SystemExit(f"UVQ ONNX model not found: {args.onnx} (export it with uvq_trt/export_onnx.py)")
    # The UI comes up first: loading the model (or building a TensorRT engine) can take minutes.
    store = ScoreStore(max_frames=args.history_frames)
    store.update_status(stream_id=args.stream_id, input_url=args.input_url, low_score=args.low_score,
                        phase="Loading the UVQ model (a TensorRT engine is built on first start on a GPU)")
    if args.http_port:
        serve(store, args.http_host, args.http_port)
    scorer = Scorer(args.device, args.onnx, args.engine, args.engine_cache_dir, args.fps, args.threads,
                    args.batch_size, args.transpose, args.ffmpeg_path)
    store.update_status(phase=None)
    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    processor = Processor(args, scorer, store)
    log.info("Input: %s (stream '%s')", args.input_url, args.stream_id)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        processor.run()
    except KeyboardInterrupt:
        log.info("Stopping...")
    processor.stop()
    shutil.rmtree(processor.reader.workdir, ignore_errors=True)
    log.info("Scored %d segments (%d skipped, %d errors)", processor.counts["ok"],
             processor.counts["skipped"], processor.counts["error"])


if __name__ == "__main__":
    main()

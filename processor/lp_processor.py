"""LivePulse processor: scores ingested segments with UVQ 1.5.

Follows the segments.jsonl manifest written by ingestor/lp_ingestor.py and
scores every new segment with Google's UVQ 1.5 model, on an NVIDIA GPU with
TensorRT (--engine) or on the CPU with ONNX Runtime (--onnx); see
uvq_trt/README.md for building those. The model is loaded once. Each segment
gives one JSON line in the scores file (default <segments_dir>/scores.jsonl),
which the monitor (monitor/lp_monitor.py) follows:

    {"stream": "cam1", "segment": "seg_..._00004.mp4", "status": "ok",
     "uvq1p5_score": 4.62, "finished_at": "...", "scored_at": "...", ...}

status is "ok", "error" (segment unreadable or deleted) or "skipped"
(dropped by --max_backlog). Segments already in the scores file are not
scored again, so the processor can be restarted at any time.

Examples:
    python processor/lp_processor.py live/cam1 --engine processor/uvq_trt/uvq1p5_fp32.engine
    python processor/lp_processor.py live/cam1 --onnx processor/uvq_trt/uvq1p5.onnx --max_backlog 6
"""

import argparse
import collections
import datetime
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "uvq_trt"))
import uvq1p5_trt  # noqa: E402
import uvq_trt_inference  # noqa: E402

MANIFEST_NAME = "segments.jsonl"
SCORES_NAME = "scores.jsonl"


def parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n\n".join(__doc__.split("\n\n")[1:]),
    )
    parser.add_argument("segments_dir", help="Ingestor --out_dir (holds the segments and segments.jsonl)")
    backend = parser.add_mutually_exclusive_group(required=True)
    backend.add_argument("--engine", help="TensorRT engine: score on the GPU")
    backend.add_argument("--onnx", help="ONNX file from uvq_trt/export_onnx.py: score on the CPU")
    parser.add_argument("--output", default=None, help="Scores file (default <segments_dir>/scores.jsonl)")
    parser.add_argument(
        "--max_backlog", type=int, default=0,
        help="If more than N segments wait for scoring, skip the oldest so scores stay current (0 = never skip)",
    )
    parser.add_argument("--max_segments", type=int, default=0, help="Stop after scoring N segments (0 = run forever)")
    parser.add_argument("--poll_interval", type=float, default=0.5, help="Seconds between manifest checks")
    score = parser.add_argument_group("UVQ options (as in uvq_trt/uvq_trt_inference.py)")
    score.add_argument("--uvq_dir", default=uvq1p5_trt.DEFAULT_UVQ_DIR, help="Path to a google/uvq checkout")
    score.add_argument("--fps", type=int, default=1, help="Frames scored per second; -1 = all (default 1)")
    score.add_argument(
        "--transpose", choices=["auto", "on", "off"], default="auto",
        help="Transpose frames before scoring; auto = for portrait video (default auto)",
    )
    score.add_argument("--batch_size", type=int, default=None, help="Frames per batch (default: engine max / 1)")
    score.add_argument("--threads", type=int, default=None, help="CPU threads for --onnx (default: all cores)")
    score.add_argument("--hwaccel", default=None, help="FFmpeg -hwaccel for decoding segments")
    score.add_argument("--ffmpeg_path", default="ffmpeg")
    score.add_argument("--ffprobe_path", default="ffprobe")
    args = parser.parse_args()
    if args.output is None:
        args.output = os.path.join(args.segments_dir, SCORES_NAME)
    return args


def log(message):
    print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


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
        self.partial = lines.pop()  # incomplete last line, if any
        records = []
        for line in lines:
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    log(f"Ignoring malformed line in {self.path}: {line[:80]!r}")
        return records


class Processor:
    def __init__(self, args):
        self.args = args
        if args.onnx:
            self.engine = uvq1p5_trt.ORTEngine(args.onnx, args.threads)
            self.backend = "onnxruntime-cpu"
            model = args.onnx
        else:
            import torch

            if not torch.cuda.is_available():
                raise SystemExit("--engine needs a CUDA GPU and a CUDA build of torch (use --onnx for the CPU)")
            self.engine = uvq1p5_trt.TRTEngine(args.engine)
            self.backend = "tensorrt-gpu"
            model = args.engine
        if args.batch_size is None:
            args.batch_size = self.engine.default_batch_size
        log(f"Scoring with {self.backend} ({os.path.basename(model)})")
        self.done = self.already_scored()
        self.pending = collections.deque()
        self.scored = 0

    def already_scored(self):
        done = set()
        for record in JsonlTail(self.args.output).read_new():
            done.add((record.get("stream"), record.get("segment")))
        if done:
            log(f"{len(done)} segments already in {self.args.output}; they won't be scored again")
        return done

    def transpose_for(self, path):
        if self.args.transpose == "auto":
            return uvq_trt_inference.is_portrait(path, self.args.ffprobe_path)
        return self.args.transpose == "on"

    def write(self, entry, status, **fields):
        record = {
            "stream": entry.get("stream"),
            "segment": entry["segment"],
            "status": status,
            "finished_at": entry.get("finished_at"),
            "duration": entry.get("duration"),
            "scored_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "model": "uvq1p5",
            "backend": self.backend,
            **fields,
        }
        with open(self.args.output, "a") as f:
            f.write(json.dumps(record) + "\n")
        self.done.add((entry.get("stream"), entry["segment"]))

    def score(self, entry):
        path = os.path.join(self.args.segments_dir, entry["segment"])
        name = entry["segment"]
        if not os.path.exists(path):
            log(f"{name}: file is gone (deleted by the ingestor's --keep_duration?)")
            self.write(entry, "error", error="segment file not found")
            return
        start = time.time()
        try:
            transpose = self.transpose_for(path)
            results = uvq_trt_inference.score_video(self.engine, path, self.args, transpose)
        except Exception as e:  # a bad segment must not stop the processor
            log(f"{name}: scoring failed: {e}")
            self.write(entry, "error", error=str(e))
            return
        elapsed = time.time() - start
        results.pop("video_name", None)
        self.write(entry, "ok", transpose=transpose, scoring_seconds=round(elapsed, 3), **results)
        self.scored += 1
        slow = "  (slower than real time)" if entry.get("duration") and elapsed > entry["duration"] else ""
        log(f"{entry.get('stream')}/{name}  UVQ {results['uvq1p5_score']:.4f}  "
            f"[{len(results['per_frame_scores'])} frames in {elapsed:.1f}s, backlog {len(self.pending)}]{slow}")

    def run(self):
        manifest = JsonlTail(os.path.join(self.args.segments_dir, MANIFEST_NAME))
        log(f"Following {manifest.path} -> {self.args.output}")
        while True:
            for entry in manifest.read_new():
                if "segment" in entry and (entry.get("stream"), entry["segment"]) not in self.done:
                    self.pending.append(entry)
            if not self.pending:
                time.sleep(self.args.poll_interval)
                continue
            while self.args.max_backlog and len(self.pending) > self.args.max_backlog:
                entry = self.pending.popleft()
                log(f"Behind by {len(self.pending) + 1} segments: skipping {entry['segment']}")
                self.write(entry, "skipped")
            self.score(self.pending.popleft())
            if self.args.max_segments and self.scored >= self.args.max_segments:
                return


def main():
    args = parse_args()
    uvq1p5_trt.add_uvq_to_path(args.uvq_dir)
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    processor = Processor(args)
    try:
        processor.run()
    except KeyboardInterrupt:
        log("Stopping (Ctrl+C)")
    log(f"Scored {processor.scored} segments this run")


if __name__ == "__main__":
    main()

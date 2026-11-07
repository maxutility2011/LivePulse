"""Continuous UVQ 1.5 scoring of a live stream.

FFmpeg records the input (anything FFmpeg can open: SRT, RTMP, UDP/RTP
MPEG-TS incl. multicast, HLS, RTSP, a capture device, ...; live MPEG-DASH
only partly, see the README) into fixed-length MP4 segments, and
every finished segment is scored with uvq_trt_inference.py's pipeline, on
the CPU with ONNX Runtime (--onnx) or on an NVIDIA GPU with TensorRT
(--engine). The model is loaded once and stays loaded, so a segment costs
only its decoding and inference time; recording never waits for scoring.

Each scored segment prints one line and, with --output, appends one JSON
line (score, per-frame scores, timing) to a .jsonl file.

Examples:
    python uvq_trt/uvq_live.py "srt://host:9000" --engine uvq_trt/uvq1p5_fp32.engine --output live.jsonl
    python uvq_trt/uvq_live.py rtmp://host/live/key --onnx uvq_trt/uvq1p5.onnx --segment_seconds 30
    python uvq_trt/uvq_live.py "udp://239.1.1.1:5000?overrun_nonfatal=1&fifo_size=50000000" --engine uvq_trt/uvq1p5_fp32.engine
    python uvq_trt/uvq_live.py https://host/live/master.m3u8 --engine uvq_trt/uvq1p5_fp32.engine --keep_duration 2h
    # Simulate a live stream from a file:
    python uvq_trt/uvq_live.py video.mp4 --input_args="-re -stream_loop -1" --onnx uvq_trt/uvq1p5.onnx
"""

import argparse
import csv
import datetime
import json
import os
import queue
import shlex
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import uvq1p5_trt  # noqa: E402
import uvq_trt_inference  # noqa: E402


def parse_duration(text):
    """'90' / '90s' / '30m' / '2h' / '7d' -> seconds."""
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    text = text.strip().lower()
    try:
        if text and text[-1] in units:
            seconds = float(text[:-1]) * units[text[-1]]
        else:
            seconds = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid duration {text!r} (use e.g. 90s, 30m, 2h, 7d)")
    if seconds < 0:
        raise argparse.ArgumentTypeError("duration must not be negative")
    return seconds


def parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n\n".join(__doc__.split("\n\n")[1:]),
    )
    parser.add_argument("input", help="Live input URL or device, anything FFmpeg's -i accepts")
    backend = parser.add_mutually_exclusive_group(required=True)
    backend.add_argument("--engine", help="TensorRT engine: score on the GPU")
    backend.add_argument("--onnx", help="ONNX file from export_onnx.py: score on the CPU with ONNX Runtime")

    rec = parser.add_argument_group("recording")
    rec.add_argument("--segment_seconds", type=float, default=10.0, help="Segment length (default 10)")
    rec.add_argument("--segment_dir", default="uvq_live_segments", help="Where MP4 segments are written")
    rec.add_argument(
        "--keep_duration", type=parse_duration, default=None,
        help="Delete segments older than this, e.g. 90s, 30m, 2h, 7d (plain number = seconds); "
             "0 = delete each segment right after scoring (default: keep all)",
    )
    rec.add_argument(
        "--map", default="0:v:0",
        help="FFmpeg -map for the video to record (default 0:v:0, the first video stream). For HLS/DASH "
             "with several renditions pick one, e.g. 0:p:2:v:0, or pass the variant playlist URL instead",
    )
    rec.add_argument(
        "--reencode", action="store_true",
        help="Re-encode (libx264, CRF --reencode_crf) instead of stream copy: exact segment lengths and "
             "works for inputs MP4 can't hold, but adds (small) coding loss to what is scored",
    )
    rec.add_argument("--reencode_crf", type=int, default=12, help="x264 CRF for --reencode (default 12)")
    rec.add_argument("--input_args", default="", help='Extra FFmpeg input options, e.g. "-rtsp_transport tcp"')
    rec.add_argument("--max_segments", type=int, default=0, help="Stop after scoring N segments (0 = run forever)")
    rec.add_argument(
        "--reconnect_delay", type=float, default=5.0,
        help="Seconds before restarting FFmpeg when the input drops; negative = exit instead (default 5)",
    )

    score = parser.add_argument_group("scoring (same meaning as in uvq_trt_inference.py)")
    score.add_argument("--uvq_dir", default=uvq1p5_trt.DEFAULT_UVQ_DIR, help="Path to a google/uvq checkout")
    score.add_argument("--fps", type=int, default=1, help="Frames scored per second; -1 = all (default 1)")
    score.add_argument(
        "--transpose", choices=["auto", "on", "off"], default="auto",
        help="Transpose frames before scoring; auto = for portrait video (default auto)",
    )
    score.add_argument("--batch_size", type=int, default=None, help="Frames per batch (default: as uvq_trt_inference)")
    score.add_argument("--threads", type=int, default=None, help="CPU threads for --onnx (default: all cores)")
    score.add_argument("--hwaccel", default=None, help="FFmpeg -hwaccel used when decoding segments for scoring")
    score.add_argument(
        "--max_backlog", type=int, default=0,
        help="If more than N finished segments wait for scoring, skip the oldest (0 = never skip)",
    )
    score.add_argument("--output", default="", help="Append one JSON line per scored segment to this file")
    score.add_argument("--ffmpeg_path", default="ffmpeg")
    score.add_argument("--ffprobe_path", default="ffprobe")
    args = parser.parse_args()
    if args.segment_seconds <= 0:
        parser.error("--segment_seconds must be positive")
    return args


def log(message):
    print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


class Recorder:
    """Runs FFmpeg's segment muxer and reports each segment once it is closed.

    FFmpeg appends a line to the CSV segment list only after a segment is
    finalized, so a listed MP4 is always complete and safe to score.
    """

    def __init__(self, args, on_segment):
        self.args = args
        self.on_segment = on_segment
        self.process = None
        self.stopping = threading.Event()

    def command(self, list_path, name_pattern):
        a = self.args
        cmd = [a.ffmpeg_path, "-hide_banner", "-loglevel", "error", "-nostats"]
        cmd += shlex.split(a.input_args, posix=os.name != "nt")
        cmd += ["-i", a.input, "-map", a.map, "-an", "-sn", "-dn"]
        if a.reencode:
            # Keyframes exactly on segment boundaries -> exact segment lengths.
            cmd += [
                "-c:v", "libx264", "-preset", "veryfast", "-crf", str(a.reencode_crf), "-pix_fmt", "yuv420p",
                "-force_key_frames", f"expr:gte(t,n_forced*{a.segment_seconds})",
            ]
        else:
            # Stream copy: no quality change, but cuts happen at the next keyframe.
            cmd += ["-c:v", "copy"]
        cmd += [
            "-f", "segment", "-segment_time", str(a.segment_seconds), "-segment_format", "mp4",
            "-reset_timestamps", "1",
            "-segment_list", list_path, "-segment_list_type", "csv",
            os.path.join(a.segment_dir, name_pattern),
        ]
        return cmd

    def run(self):
        while not self.stopping.is_set():
            run_start = time.time()
            # Names are <FFmpeg start time>_<counter>, not a per-segment
            # timestamp: inputs like HLS deliver their first segments faster than
            # real time, so several segments can start within the same second.
            now = datetime.datetime.now()
            stamp = f"{now:%Y%m%d-%H%M%S}-{now.microsecond // 1000:03d}"
            list_path = os.path.join(self.args.segment_dir, f"segments_{stamp}.csv")
            open(list_path, "w").close()
            log(f"Recording {self.args.input} into {self.args.segment_seconds:g}s segments in {self.args.segment_dir}")
            self.process = subprocess.Popen(self.command(list_path, f"seg_{stamp}_%05d.mp4"), stdin=subprocess.PIPE)
            self.follow_list(list_path)
            code = self.process.wait()
            os.remove(list_path)
            if self.stopping.is_set():
                break
            log(f"FFmpeg exited with code {code} after {time.time() - run_start:.0f}s")
            if self.args.reconnect_delay < 0:
                break
            log(f"Reconnecting in {self.args.reconnect_delay:g}s")
            if self.stopping.wait(self.args.reconnect_delay):
                break
        self.on_segment(None)  # tells the scorer that no more segments will come

    def follow_list(self, list_path):
        """Tails the segment list until FFmpeg exits, reporting new segments."""
        seen = 0
        while True:
            exited = self.process.poll() is not None
            with open(list_path, newline="") as f:
                rows = [r for r in csv.reader(f) if len(r) >= 3]
            for name, start, end in (r[:3] for r in rows[seen:]):
                self.on_segment({
                    "path": os.path.join(self.args.segment_dir, os.path.basename(name)),
                    "duration": float(end) - float(start),
                    "finished_at": datetime.datetime.now(),
                })
            seen = len(rows)
            if exited:
                return
            time.sleep(0.2)

    def stop(self):
        """Asks FFmpeg to finish cleanly ('q'), so the open segment gets a valid moov box."""
        self.stopping.set()
        if self.process and self.process.poll() is None:
            try:
                self.process.stdin.write(b"q")
                self.process.stdin.flush()
                self.process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                self.process.terminate()


class Scorer:
    def __init__(self, args):
        self.args = args
        if args.onnx:
            self.engine = uvq1p5_trt.ORTEngine(args.onnx, args.threads)
            device = f"CPU (ONNX Runtime, {os.path.basename(args.onnx)})"
        else:
            import torch

            if not torch.cuda.is_available():
                raise SystemExit("--engine needs a CUDA GPU and a CUDA build of torch (use --onnx for the CPU)")
            self.engine = uvq1p5_trt.TRTEngine(args.engine)
            device = f"GPU (TensorRT, {os.path.basename(args.engine)})"
        if args.batch_size is None:
            args.batch_size = self.engine.default_batch_size
        log(f"Scoring on {device}")
        self.queue = queue.Queue()
        self.started_at = time.time()
        self.done = set()  # segments of this run that are scored or skipped
        self.scored = 0
        self.all_scores = []

    def submit(self, segment):
        self.queue.put(segment)

    def transpose_for(self, path):
        if self.args.transpose == "auto":
            return uvq_trt_inference.is_portrait(path, self.args.ffprobe_path)
        return self.args.transpose == "on"

    def retire(self, path):
        """Marks a segment done (scored or skipped) and applies --keep_duration."""
        self.done.add(path)
        if self.args.keep_duration is None:
            return
        # A segment's age is taken from its file's last write, i.e. when FFmpeg
        # finished it. Only segments that are done, or left over from earlier
        # runs, are deleted - never one still being written or waiting.
        cutoff = time.time() - self.args.keep_duration
        for entry in os.scandir(self.args.segment_dir):
            if not (entry.name.startswith("seg_") and entry.name.endswith(".mp4")):
                continue
            try:
                mtime = entry.stat().st_mtime
                if (entry.path in self.done or mtime < self.started_at) and mtime <= cutoff:
                    os.remove(entry.path)
                    self.done.discard(entry.path)
            except OSError as e:
                log(f"Could not delete {entry.name}: {e}")

    def run(self, recorder):
        while True:
            segment = self.queue.get()
            if segment is None:
                return
            backlog = self.queue.qsize()
            if self.args.max_backlog and backlog > self.args.max_backlog:
                log(f"Scoring is behind ({backlog} segments waiting): skipping {os.path.basename(segment['path'])}")
                self.retire(segment["path"])
                continue
            self.score(segment, backlog)
            if self.args.max_segments and self.scored >= self.args.max_segments:
                recorder.stop()
                # Wait for FFmpeg to exit. Segments it still closes aren't scored
                # but fall under --keep_duration like skipped ones.
                while (leftover := self.queue.get()) is not None:
                    self.retire(leftover["path"])
                return

    def score(self, segment, backlog):
        path = segment["path"]
        name = os.path.basename(path)
        start = time.time()
        try:
            transpose = self.transpose_for(path)
            results = uvq_trt_inference.score_video(self.engine, path, self.args, transpose)
        except Exception as e:  # a bad segment must not stop the monitor
            log(f"{name}: scoring failed: {e}")
            self.retire(path)
            return
        elapsed = time.time() - start
        score = results["uvq1p5_score"]
        self.scored += 1
        self.all_scores.append(score)
        warn = "  (slower than real time)" if elapsed > segment["duration"] else ""
        log(
            f"{name}  {segment['duration']:5.1f}s  UVQ {score:.4f}  "
            f"[{len(results['per_frame_scores'])} frames, scored in {elapsed:.1f}s, backlog {backlog}]{warn}"
        )
        if self.args.output:
            record = {
                "segment": name,
                "finished_at": segment["finished_at"].isoformat(timespec="seconds"),
                "duration": round(segment["duration"], 3),
                "transpose": transpose,
                **results,
                "scoring_seconds": round(elapsed, 3),
            }
            record.pop("video_name", None)
            with open(self.args.output, "a") as f:
                f.write(json.dumps(record) + "\n")
        self.retire(path)


def main():
    args = parse_args()
    uvq1p5_trt.add_uvq_to_path(args.uvq_dir)
    os.makedirs(args.segment_dir, exist_ok=True)
    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)

    scorer = Scorer(args)
    recorder = Recorder(args, scorer.submit)
    record_thread = threading.Thread(target=recorder.run, daemon=True)
    record_thread.start()
    try:
        scorer.run(recorder)
    except KeyboardInterrupt:
        log("Stopping (Ctrl+C)...")
        recorder.stop()
    record_thread.join(timeout=15)

    if scorer.all_scores:
        mean = sum(scorer.all_scores) / len(scorer.all_scores)
        log(f"Scored {scorer.scored} segments, mean UVQ {mean:.4f} "
            f"(min {min(scorer.all_scores):.4f}, max {max(scorer.all_scores):.4f})")


if __name__ == "__main__":
    main()

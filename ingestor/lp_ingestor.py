"""LivePulse ingestor: records a live stream into fragmented-MP4 segments.

FFmpeg reads the input (anything it can open: SRT, RTMP, UDP/RTP MPEG-TS
incl. multicast, HLS, RTSP, a capture device, ...; live MPEG-DASH only
partly, see README.md) and cuts it into fixed-length fragmented MP4 (fMP4)
segments. Every finished segment is announced with one JSON line appended to
<out_dir>/segments.jsonl, which the processor (processor/lp_processor.py)
follows:

    {"stream": "cam1", "segment": "seg_20261107-120000-123_00004.mp4",
     "duration": 10.0, "finished_at": "2026-11-07T12:00:52"}

A line is only written once FFmpeg has closed the segment, so every listed
file is complete.

Examples:
    python ingestor/lp_ingestor.py "srt://host:9000" --stream_id cam1 --out_dir live/cam1
    python ingestor/lp_ingestor.py rtmp://host/live/key --out_dir live/key --keep_duration 2h
    python ingestor/lp_ingestor.py "udp://239.1.1.1:5000?overrun_nonfatal=1&fifo_size=50000000" --out_dir live/mc1
    # Simulate a live stream from a file:
    python ingestor/lp_ingestor.py video.mp4 --input_args="-re -stream_loop -1" --out_dir live/test
"""

import argparse
import csv
import datetime
import json
import os
import shlex
import subprocess
import sys
import threading
import time

MANIFEST_NAME = "segments.jsonl"


def parse_duration(text):
    """'90' / '90s' / '30m' / '2h' / '7d' -> seconds."""
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    text = text.strip().lower()
    try:
        seconds = float(text[:-1]) * units[text[-1]] if text and text[-1] in units else float(text)
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
    parser.add_argument("--out_dir", default="live", help="Where segments and segments.jsonl go (default live)")
    parser.add_argument("--stream_id", default=None, help="Name of this stream in all records (default: out_dir name)")
    parser.add_argument("--segment_seconds", type=float, default=10.0, help="Segment length (default 10)")
    parser.add_argument(
        "--keep_duration", type=parse_duration, default=None,
        help="Delete segments older than this, e.g. 90s, 30m, 2h, 7d (plain number = seconds) "
             "(default: keep all). Must exceed how far the processor may fall behind",
    )
    parser.add_argument(
        "--map", default="0:v:0",
        help="FFmpeg -map for the video to record (default 0:v:0). For HLS/DASH with several renditions "
             "pick one, e.g. 0:p:2:v:0, or pass the variant playlist URL",
    )
    parser.add_argument(
        "--reencode", action="store_true",
        help="Re-encode (libx264, CRF --reencode_crf) instead of stream copy: exact segment lengths and "
             "works for inputs MP4 can't hold, but adds (small) coding loss",
    )
    parser.add_argument("--reencode_crf", type=int, default=12, help="x264 CRF for --reencode (default 12)")
    parser.add_argument("--input_args", default="", help='Extra FFmpeg input options, e.g. --input_args="-re"')
    parser.add_argument(
        "--reconnect_delay", type=float, default=5.0,
        help="Seconds before restarting FFmpeg when the input drops; negative = exit instead (default 5)",
    )
    parser.add_argument("--max_segments", type=int, default=0, help="Stop after N segments (0 = run forever)")
    parser.add_argument("--ffmpeg_path", default="ffmpeg")
    args = parser.parse_args()
    if args.segment_seconds <= 0:
        parser.error("--segment_seconds must be positive")
    if args.stream_id is None:
        args.stream_id = os.path.basename(os.path.normpath(os.path.abspath(args.out_dir)))
    return args


def log(message):
    print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


class Ingestor:
    """Runs FFmpeg's segment muxer and announces each closed segment.

    FFmpeg appends a row to its CSV segment list only after a segment is
    finalized; that list is followed here and turned into manifest lines.
    """

    def __init__(self, args):
        self.args = args
        self.manifest = os.path.join(args.out_dir, MANIFEST_NAME)
        self.process = None
        self.stopping = threading.Event()
        self.count = 0
        self.started_at = time.time()
        self.done = set()  # finished segments of this run

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
            # Stream copy: exactly what was received, but cuts happen at keyframes.
            cmd += ["-c:v", "copy"]
        cmd += [
            "-f", "segment", "-segment_time", str(a.segment_seconds), "-reset_timestamps", "1",
            "-segment_format", "mp4",
            # Fragmented MP4: moov up front, then moof/mdat per keyframe.
            "-segment_format_options", "movflags=+frag_keyframe+empty_moov+default_base_moof",
            "-segment_list", list_path, "-segment_list_type", "csv",
            os.path.join(a.out_dir, name_pattern),
        ]
        return cmd

    def run(self):
        while not self.stopping.is_set():
            run_start = time.time()
            # Names are <FFmpeg start time>_<counter>: inputs like HLS deliver
            # their first segments faster than real time, so per-segment
            # timestamps would collide.
            now = datetime.datetime.now()
            stamp = f"{now:%Y%m%d-%H%M%S}-{now.microsecond // 1000:03d}"
            list_path = os.path.join(self.args.out_dir, f".ffmpeg_list_{stamp}.csv")
            open(list_path, "w").close()
            log(f"Recording {self.args.input} as stream '{self.args.stream_id}' into "
                f"{self.args.segment_seconds:g}s fMP4 segments in {self.args.out_dir}")
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

    def follow_list(self, list_path):
        """Tails FFmpeg's segment list until FFmpeg exits."""
        seen = 0
        while True:
            exited = self.process.poll() is not None
            with open(list_path, newline="") as f:
                rows = [r for r in csv.reader(f) if len(r) >= 3]
            for name, start, end in (r[:3] for r in rows[seen:]):
                self.announce(os.path.basename(name), float(end) - float(start))
            seen = len(rows)
            if exited:
                return
            time.sleep(0.2)

    def announce(self, name, duration):
        record = {
            "stream": self.args.stream_id,
            "segment": name,
            "duration": round(duration, 3),
            "finished_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        with open(self.manifest, "a") as f:
            f.write(json.dumps(record) + "\n")
        self.count += 1
        self.done.add(os.path.join(self.args.out_dir, name))
        log(f"{name}  {duration:5.1f}s")
        self.apply_retention()
        if self.args.max_segments and self.count >= self.args.max_segments:
            threading.Thread(target=self.stop, daemon=True).start()

    def apply_retention(self):
        """Deletes finished segments (and leftovers of earlier runs) older than --keep_duration."""
        if self.args.keep_duration is None:
            return
        cutoff = time.time() - self.args.keep_duration
        for entry in os.scandir(self.args.out_dir):
            if not (entry.name.startswith("seg_") and entry.name.endswith(".mp4")):
                continue
            try:
                mtime = entry.stat().st_mtime  # last write = when FFmpeg closed it
                if (entry.path in self.done or mtime < self.started_at) and mtime <= cutoff:
                    os.remove(entry.path)
                    self.done.discard(entry.path)
            except OSError as e:
                log(f"Could not delete {entry.name}: {e}")

    def stop(self):
        """Asks FFmpeg to finish cleanly ('q') so the open segment is completed."""
        self.stopping.set()
        if self.process and self.process.poll() is None:
            try:
                self.process.stdin.write(b"q")
                self.process.stdin.flush()
                self.process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                self.process.terminate()


def main():
    args = parse_args()
    os.makedirs(args.out_dir, exist_ok=True)
    ingestor = Ingestor(args)
    # Recording runs in a worker thread so that on Ctrl+C the main thread can
    # stop FFmpeg while the worker still announces the segment it closes.
    worker = threading.Thread(target=ingestor.run, daemon=True)
    worker.start()
    try:
        while worker.is_alive():
            worker.join(0.5)
    except KeyboardInterrupt:
        log("Stopping (Ctrl+C)...")
        ingestor.stop()
        worker.join(15)
    log(f"Recorded {ingestor.count} segments")
    return 0


if __name__ == "__main__":
    sys.exit(main())

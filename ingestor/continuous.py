"""Continuous inputs (RTMP / SRT / UDP-RTP MPEG-TS): GOP-aligned HLS without re-encoding.

FFmpeg's HLS muxer cuts against cumulative targets (5, 10, 15 s, ...), so
with a 2 s GOP and 5 s segments it alternates 6 s and 4 s segments. Instead:

1. FFmpeg stream-copies the input into fMP4 HLS with a tiny hls_time, which
   makes it cut at *every* keyframe: one fragment (moof+mdat) per GOP, all
   sharing one init segment, with exact GOP durations in its playlist.
2. The packager concatenates the fewest consecutive GOPs whose total length
   reaches --segment_seconds into one media segment (several moof/mdat pairs
   in a segment are valid fMP4/CMAF), e.g. 3 x 2 s GOPs = 6 s for 5 s.

Each FFmpeg run (a reconnect starts a new one) gets its own init segment,
and its first output segment is flagged as a discontinuity.
"""

import datetime
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
import time

from hls_output import Segment

log = logging.getLogger("ingestor")

GOP_PLAYLIST = "gops.m3u8"
EPSILON = 1e-3  # seconds; absorbs EXTINF rounding


def parse_gop_playlist(text):
    """Returns [(sequence, filename, duration)] from FFmpeg's per-GOP playlist."""
    seq = 0
    m = re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", text)
    if m:
        seq = int(m.group(1))
    entries, duration = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            duration = float(line[8:].split(",")[0])
        elif line and not line.startswith("#") and duration is not None:
            entries.append((seq, line, duration))
            seq += 1
            duration = None
    return entries


class ContinuousIngest:
    def __init__(self, args, input_args, output):
        self.args = args
        self.input_args = input_args  # FFmpeg arguments up to and including -i URL
        self.output = output
        self.stopping = threading.Event()
        self.process = None
        self.run_index = 0
        self.segments = 0

    def ffmpeg_command(self, stage):
        a = self.args
        stage = stage.replace("\\", "/")  # FFmpeg's hls muxer locates the init file by '/'
        cmd = [a.ffmpeg_path, "-hide_banner", "-loglevel", "error", "-nostats"]
        cmd += shlex.split(a.input_args, posix=os.name != "nt")
        cmd += self.input_args
        cmd += ["-map", "0:v:0"]
        if a.audio == "copy":
            # MPEG-TS carries AAC as ADTS, which fMP4 can't hold as is.
            cmd += ["-map", "0:a:0?", "-c:v", "copy", "-c:a", "copy", "-bsf:a", "aac_adtstoasc"]
        elif a.audio == "aac":
            cmd += ["-map", "0:a:0?", "-c:v", "copy", "-c:a", "aac", "-b:a", a.audio_bitrate]
        else:
            cmd += ["-an", "-c:v", "copy"]
        cmd += [
            "-sn", "-dn", "-f", "hls",
            "-hls_time", "0.001",  # cut at every keyframe -> one fragment per GOP
            "-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", "init.mp4",
            "-hls_list_size", "100", "-hls_flags", "independent_segments",
            "-hls_segment_filename", f"{stage}/gop_%08d.m4s",
            f"{stage}/{GOP_PLAYLIST}",
        ]
        return cmd

    def run(self):
        last_exit_log = 0.0
        while not self.stopping.is_set():
            self.run_index += 1
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            stage = os.path.join(self.args.staging_dir, self.args.stream_id, f"run_{stamp}_{self.run_index}")
            os.makedirs(stage, exist_ok=True)
            start = time.time()
            log.info("Waiting for / reading input (FFmpeg run %d)", self.run_index)
            self.process = subprocess.Popen(self.ffmpeg_command(stage), stdin=subprocess.PIPE)
            self.package(stage)
            code = self.process.wait()
            shutil.rmtree(stage, ignore_errors=True)
            if self.stopping.is_set():
                break
            # Listening inputs with a stall timeout restart often while idle; don't flood the log.
            if time.time() - last_exit_log > 60 or time.time() - start > 30:
                log.info("FFmpeg exited with code %s after %.0fs", code, time.time() - start)
                last_exit_log = time.time()
            if self.args.reconnect_delay < 0:
                break
            if self.stopping.wait(self.args.reconnect_delay):
                break

    def package(self, stage):
        """Follows FFmpeg's per-GOP playlist and emits GOP-aligned segments."""
        playlist = os.path.join(stage, GOP_PLAYLIST)
        last_seq = -1
        pending = []  # [(filename, duration)] GOPs of the segment being built
        init_name = None
        first_of_run = True
        while True:
            exited = self.process.poll() is not None
            try:
                with open(playlist) as f:
                    entries = parse_gop_playlist(f.read())
            except FileNotFoundError:
                entries = []
            new = [e for e in entries if e[0] > last_seq]
            if new and last_seq >= 0 and new[0][0] != last_seq + 1:
                log.warning("Missed GOPs %d-%d (packager fell behind)", last_seq + 1, new[0][0] - 1)
            for seq, name, duration in new:
                last_seq = seq
                if init_name is None:
                    init_name = f"init_{self.run_index:04d}.mp4"
                    self.output.add_init(init_name, os.path.join(stage, "init.mp4"))
                pending.append((name, duration))
                if sum(d for _, d in pending) >= self.args.segment_seconds - EPSILON:
                    self.emit(stage, pending, init_name, first_of_run)
                    pending, first_of_run = [], False
            if exited:
                if pending:  # the stream ended mid-segment: publish what there is
                    self.emit(stage, pending, init_name, first_of_run)
                return
            time.sleep(0.2)

    def emit(self, stage, gops, init_name, first_of_run):
        self.segments += 1
        name = f"seg_{self.segments:08d}.m4s"
        # Built in the output's temp dir: the stage dir is removed when FFmpeg
        # exits, possibly before the upload thread has published this segment.
        joined = os.path.join(self.output.tmpdir, name)
        with open(joined, "wb") as out:
            for gop, _ in gops:
                gop_path = os.path.join(stage, gop)
                with open(gop_path, "rb") as f:
                    shutil.copyfileobj(f, out)
                os.remove(gop_path)
        duration = sum(d for _, d in gops)
        self.output.add_segment(
            Segment(name, duration, init=init_name, discontinuity=first_of_run and self.run_index > 1),
            joined, move=True,
        )
        log.info("%s  %.3fs (%d GOP%s)", name, duration, len(gops), "" if len(gops) == 1 else "s")

    def stop(self):
        """Asks FFmpeg to finish cleanly ('q'); the packager publishes what's left."""
        self.stopping.set()
        if self.process and self.process.poll() is None:
            try:
                self.process.stdin.write(b"q")
                self.process.stdin.flush()
                self.process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                self.process.terminate()

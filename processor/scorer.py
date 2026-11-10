"""UVQ 1.5 scoring of HLS segments on the GPU (TensorRT) or the CPU (ONNX Runtime).

Device: --device auto uses the GPU when CUDA and TensorRT are available,
otherwise the CPU. On the GPU a TensorRT engine is needed; it is specific to
the GPU model and TensorRT version, so it is built from the ONNX model on
first use and cached (--engine_cache_dir).

Frames: segments are decoded with UVQ's own FFmpeg pipeline (fps sampling,
bicubic scale to 1920x1080, RGB), but - unlike UVQ, which pads videos to a
whole number of seconds with black frames - only the frames that really come
out of FFmpeg are scored. Scores are produced batch by batch, so callers can
publish them before the whole segment is done.
"""

import logging
import os
import re
import subprocess
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "uvq_trt"))
import uvq1p5_trt  # noqa: E402

log = logging.getLogger("processor")
DEFAULT_FPS = {"gpu": 5, "cpu": 1}
FRAME_BYTES = uvq1p5_trt.VIDEO_WIDTH * uvq1p5_trt.VIDEO_HEIGHT * uvq1p5_trt.VIDEO_CHANNELS


def gpu_status():
    """(available, reason)."""
    try:
        import torch
    except ImportError:
        return False, "torch is not installed"
    if not torch.cuda.is_available():
        return False, "no CUDA GPU visible (on Docker: run with --gpus all and the NVIDIA Container Toolkit)"
    try:
        import tensorrt  # noqa: F401
    except ImportError:
        return False, "TensorRT is not installed"
    return True, torch.cuda.get_device_name(0)


def engine_path_for(cache_dir):
    import tensorrt as trt
    import torch

    gpu = re.sub(r"[^A-Za-z0-9]+", "-", torch.cuda.get_device_name(0)).strip("-").lower()
    return os.path.join(cache_dir, f"uvq1p5_fp32_{gpu}_trt{trt.__version__}.engine")


def ensure_engine(onnx_path, cache_dir, max_batch=8):
    """Returns a TensorRT engine for this GPU, building and caching it if needed."""
    os.makedirs(cache_dir, exist_ok=True)
    path = engine_path_for(cache_dir)
    if os.path.exists(path):
        return path
    log.info("Building a TensorRT engine for this GPU (once; takes a few minutes) -> %s", path)
    start = time.time()
    # FP32: FP16 engines return NaN (UVQ's AggregationNet overflows FP16), see uvq_trt/README.md.
    cmd = [sys.executable, os.path.join(HERE, "uvq_trt", "build_engine.py"), onnx_path, "-o", path + ".tmp",
           "--opt_batch", str(min(4, max_batch)), "--max_batch", str(max_batch),
           "--timing_cache", os.path.join(cache_dir, "timing.cache")]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not os.path.exists(path + ".tmp"):
        raise RuntimeError(f"TensorRT engine build failed:\n{result.stdout[-2000:]}\n{result.stderr[-2000:]}")
    os.replace(path + ".tmp", path)
    log.info("Engine built in %.0fs", time.time() - start)
    return path


class Scorer:
    def __init__(self, device="auto", onnx_path=None, engine_path=None, engine_cache_dir=None,
                 fps=None, threads=None, batch_size=None, transpose="auto", ffmpeg_path="ffmpeg"):
        available, reason = gpu_status()
        if device == "auto":
            device = "gpu" if available else "cpu"
            if not available:
                log.info("No usable GPU (%s); using the CPU", reason)
        elif device == "gpu" and not available:
            raise SystemExit(f"--device gpu, but {reason}")
        self.device = device
        if device == "gpu":
            engine_path = engine_path or ensure_engine(onnx_path, engine_cache_dir)
            self.engine = uvq1p5_trt.TRTEngine(engine_path)
            self.backend = f"TensorRT on {reason}"
            self.model = os.path.basename(engine_path)
        else:
            self.engine = uvq1p5_trt.ORTEngine(onnx_path, threads)
            self.backend = f"ONNX Runtime on CPU ({threads or os.cpu_count()} threads)"
            self.model = os.path.basename(onnx_path)
        self.fps = fps or DEFAULT_FPS[device]
        self.batch_size = batch_size or self.engine.default_batch_size
        self.transpose = transpose
        self.ffmpeg_path = ffmpeg_path
        log.info("Scoring with %s, %d frame(s) per second, batches of %d", self.backend, self.fps, self.batch_size)

    def is_portrait(self, path):
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=width,height:stream_side_data=rotation", "-of", "default=nw=1", path],
                             capture_output=True, text=True).stdout
        vals = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        try:
            w, h = int(vals.get("width", 0)), int(vals.get("height", 0))
        except ValueError:
            return False
        if vals.get("rotation", "0").lstrip("-") in ("90", "270"):
            w, h = h, w
        return 0 < w < h

    def frame_batches(self, path, transpose):
        """Yields uint8 batches (N, 1080, 1920, 3) of the frames FFmpeg samples - no padding."""
        transpose_filter = "transpose=2," if transpose else ""
        vf = (f"fps={self.fps}:start_time=0:round=up,{transpose_filter}"
              f"scale=w={uvq1p5_trt.VIDEO_WIDTH}:h={uvq1p5_trt.VIDEO_HEIGHT}:flags=bicubic,format=rgb24")
        cmd = [self.ffmpeg_path, "-hide_banner", "-loglevel", "error", "-i", path, "-vf", vf,
               "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            while True:
                data = proc.stdout.read(self.batch_size * FRAME_BYTES)
                n = len(data) // FRAME_BYTES
                if n:
                    frames = np.frombuffer(bytearray(data[: n * FRAME_BYTES]), dtype=np.uint8)
                    yield frames.reshape(n, uvq1p5_trt.VIDEO_HEIGHT, uvq1p5_trt.VIDEO_WIDTH, 3)
                if len(data) < self.batch_size * FRAME_BYTES:
                    break
        finally:
            proc.stdout.close()
            err = proc.stderr.read().decode(errors="replace").strip()
            proc.wait()
            if err:
                log.debug("ffmpeg: %s", err[:300])

    def score(self, path):
        """Generator: yields (first_frame_index, scores ndarray) per batch for one segment file."""
        import torch

        if self.transpose == "auto":
            transpose = self.is_portrait(path)
        else:
            transpose = self.transpose == "on"
        index = 0
        with torch.inference_mode():
            for frames in self.frame_batches(path, transpose):
                scores = self.engine(torch.from_numpy(frames)).float().cpu().numpy().reshape(-1)
                yield index, scores
                index += len(scores)

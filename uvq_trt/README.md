# UVQ 1.5 on TensorRT

Scripts to serialize Google's [UVQ](https://github.com/google/uvq) 1.5
no-reference video quality model into a TensorRT engine and run inference
with it, on video files or continuously on live streams
([Live streams](#live-streams)).

UVQ 1.5 is three PyTorch networks: ContentNet, DistortionNet (two
EfficientNet-B0 backbones) and AggregationNet. Rather than three engines,
`export_onnx.py` fuses all of them, including the input normalization, the
3x3 patch split of DistortionNet and ContentNet's 256x256 resize, into **one
ONNX graph -> one TensorRT engine**:

| tensor   | shape                    | dtype   |                                  |
|----------|--------------------------|---------|----------------------------------|
| `frames` | `(batch, 1080, 1920, 3)` | uint8   | RGB, as FFmpeg's `rgb24` output   |
| `scores` | `(batch, 1)`             | float32 | per-frame UVQ 1.5 score in [1, 5] |

Only the batch dimension is dynamic (UVQ always rescales frames to 1920x1080).
Feeding uint8 instead of float32 also cuts host->GPU traffic by 4x.

Only UVQ 1.5 (UVQ's default and recommended version) is covered, not UVQ 1.0.

The same exported ONNX graph can also run on the CPU with ONNX Runtime
(`--onnx` instead of `--engine`), no GPU or TensorRT needed - useful to check
the conversion on your own videos before building an engine.

## Requirements

- Python 3.10-3.13, `pip install -r uvq_trt/requirements.txt`
- `ffmpeg` / `ffprobe` on `PATH`
- For the TensorRT path only: an NVIDIA GPU, CUDA, and TensorRT >= 8.6
  (TensorRT 10 recommended) with its Python bindings, e.g.
  `pip install tensorrt`

Get the UVQ model code and checkpoints (cloned to `uvq_trt/uvq/`, pinned to a
known revision):

```bash
uvq_trt/fetch_uvq.sh
```

On Windows, see [Windows, CPU only](#windows-cpu-only) below instead.

## 1. Export ONNX (any machine, CPU is fine)

```bash
python3 uvq_trt/export_onnx.py -o uvq_trt/uvq1p5.onnx
```

The export is checked with ONNX Runtime against the unmodified PyTorch UVQ
model and fails if the scores differ by more than 1e-3.

| option | default | |
|---|---|---|
| `-o`, `--output FILE` | `uvq_trt/uvq1p5.onnx` | ONNX file to write |
| `--uvq_dir DIR` | `uvq_trt/uvq` | google/uvq checkout (model code + checkpoints) |
| `--opset N` | `17` | ONNX opset (>= 17 for a native LayerNormalization) |
| `--skip_verify` | off | don't compare ONNX Runtime against PyTorch |
| `--verify_batch N` | `2` | frames used for the verification run |

## 2. Build the TensorRT engine (on the GPU machine that will run it)

Engines are specific to the GPU model and TensorRT version, so build on the
inference machine:

```bash
python3 uvq_trt/build_engine.py uvq_trt/uvq1p5.onnx -o uvq_trt/uvq1p5_fp32.engine
```

> **Don't use `--fp16` for now: FP16 engines return `NaN` scores.**
> AggregationNet's `conv1` output exceeds the FP16 range (about 73,000
> measured on real video vs. a 65,504 maximum), which becomes `inf` and then
> `NaN` after its LayerNorm. Keeping just those layers in FP32 with per-layer
> precision constraints doesn't help: TensorRT 10.16 ignores them (they are
> deprecated since 10.12 in favor of strong typing). The FP32 engine matches
> PyTorch to ~5e-4 per frame, and on an RTX 4060 inference is ~25 ms per
> frame, so FFmpeg decoding and startup dominate the run time anyway.

| option | default | |
|---|---|---|
| `onnx` | (required) | ONNX file written by `export_onnx.py` |
| `-o`, `--output FILE` | next to the ONNX file | engine file to write |
| `--fp16` | off | use FP16 kernels. **Currently produces `NaN` scores (see above); leave it off** |
| `--min_batch N` | `1` | smallest batch the engine accepts |
| `--opt_batch N` | `4` | batch size TensorRT tunes kernels for |
| `--max_batch N` | `8` | largest batch the engine accepts. GPU memory grows with it (see below) |
| `--workspace_gb GB` | `4.0` | builder scratch memory limit |
| `--timing_cache FILE` | none | reuse kernel timings to make rebuilds faster |
| `--verbose` | off | verbose TensorRT builder log |

**Choosing the batch size.** The engine is sized for `--max_batch`, and each
1080p frame runs as 9 DistortionNet patches of 360x640, so GPU memory grows
quickly with the batch: at batch 24 single layers request 7-9 GB, which fails
on an 8 GB GPU with `Device memory is insufficient to use tactic` and
`Error Code 2: OutOfMemory`. Roughly:

| GPU memory | suggested `--opt_batch` / `--max_batch` |
|---|---|
| 6-8 GB (e.g. RTX 4060) | 4 / 4-8 (default 4 / 8) |
| 12-16 GB | 8 / 16 |
| 24 GB or more | 16 / 24 (UVQ's own CUDA batch size) |

`--workspace_gb` only caps scratch memory, not these allocations, so lower
the batch instead. Larger batches mostly cut per-call overhead; at batch 4
the GPU already gets 36 patches per call, and FFmpeg decoding + rescaling
often takes longer than inference anyway. Building also takes much longer for large batches.

Or with `trtexec`:

```bash
trtexec --onnx=uvq_trt/uvq1p5.onnx --saveEngine=uvq_trt/uvq1p5_fp32.engine --minShapes=frames:1x1080x1920x3 --optShapes=frames:4x1080x1920x3 --maxShapes=frames:8x1080x1920x3
```

## 3. Run inference

Same arguments and output as UVQ's `uvq_inference.py --model_version 1.5`:

```bash
python3 uvq_trt/uvq_trt_inference.py video.mp4 --engine uvq_trt/uvq1p5_fp32.engine
```

```bash
python3 uvq_trt/uvq_trt_inference.py video.mp4 --engine uvq_trt/uvq1p5_fp32.engine --output_all_stats
```

```bash
python3 uvq_trt/uvq_trt_inference.py videos.txt --engine uvq_trt/uvq1p5_fp32.engine --output scores.txt
```

```
uvq_trt_inference.py INPUT (--engine FILE | --onnx FILE) [options]
```

| option | default | |
|---|---|---|
| `input` | (required) | a video file, or a `.txt` file with one video path per line |
| `--engine FILE` | | TensorRT engine from step 2 (NVIDIA GPU). Exactly one of `--engine` / `--onnx` is required |
| `--onnx FILE` | | ONNX file from step 1, run with ONNX Runtime on the CPU (see [below](#running-on-the-cpu-with-onnx-runtime)) |
| `--uvq_dir DIR` | `uvq_trt/uvq` | google/uvq checkout (model code, checkpoints and its `utils/` for probing and decoding) |
| `--output FILE` | none | single video: also write the full results as JSON. `.txt` input: required, one `name,score` line per video |
| `--transpose` | off | transpose the video before scoring, for portrait videos. Single video only, as in UVQ; `.txt` input detects portrait videos automatically |
| `--fps N` | `1` | frames sampled per second; `-1` = every frame |
| `--batch_size N` | engine's max batch, `1` with `--onnx` | frames decoded and scored per batch |
| `--threads N` | all cores | CPU threads for ONNX Runtime (`--onnx` only) |
| `--hwaccel NAME` | none | FFmpeg `-hwaccel` for decoding, e.g. `cuda` for NVDEC. The decoder is bit-exact, so scores don't change, but it is often **not** faster: UVQ's `fps`/bicubic `scale` filters run on the CPU, so every decoded frame (all of them, not only the sampled ones) is copied back from the GPU, and NVDEC adds a fixed start-up cost. E.g. 2560x1440 H.264 on a Ryzen 7 7700 + RTX 4060 decodes in 0.6 s on the CPU vs. 0.8 s with `cuda`. Mainly useful on weak or busy CPUs, or for heavy codecs (4K HEVC/AV1) |
| `--output_all_stats` | off | print the full JSON result (`uvq1p5_score`, `per_frame_scores`, `frame_indices`, `video_name`) instead of just the score |
| `--compare_pytorch` | off | also run the original PyTorch UVQ on the same frames and device and report `pytorch_uvq1p5_score` and `max_abs_frame_diff`; use it once to check an engine's accuracy on your content (expect ~5e-4 for FP32). Slows the run down, so leave it off when timing |
| `--ffmpeg_path PATH` | `ffmpeg` | FFmpeg executable |
| `--ffprobe_path PATH` | `ffprobe` | ffprobe executable |

Decoding runs in a background thread, so it overlaps with inference.

## Live streams

`uvq_live.py` scores a live stream continuously. FFmpeg records the input
into fixed-length MP4 segments, and each finished segment is scored with the
same pipeline as `uvq_trt_inference.py`, on the GPU (`--engine`) or the CPU
(`--onnx`). The model stays loaded between segments, and recording never
waits for scoring.

```bash
python3 uvq_trt/uvq_live.py "srt://host:9000" --engine uvq_trt/uvq1p5_fp32.engine --output live.jsonl
python3 uvq_trt/uvq_live.py rtmp://host/live/key --onnx uvq_trt/uvq1p5.onnx --segment_seconds 30 --keep_duration 2h
# simulate a live stream from a file
python3 uvq_trt/uvq_live.py video.mp4 --input_args="-re -stream_loop -1" --engine uvq_trt/uvq1p5_fp32.engine
```

Each scored segment prints a line such as

```
[2026-11-06 17:12:39] seg_20261106-171232-645_00000.mp4    6.2s  UVQ 4.6234  [7 frames, scored in 1.0s, backlog 0]
```

and, with `--output`, appends a JSON line with `segment`, `finished_at`
(wall-clock time the segment was closed), `duration`, `transpose`,
`uvq1p5_score`, `per_frame_scores`, `frame_indices` and `scoring_seconds`.
Segments are named `seg_<FFmpeg start time>_<counter>.mp4`; the counter
restarts when FFmpeg reconnects. Stop with Ctrl+C (FFmpeg is asked to
finish cleanly) or `--max_segments`.

### Input formats

All of these were tested end to end with FFmpeg 9 (a local FFmpeg sender
feeding `uvq_live.py`), except live MPEG-DASH, see below:

| format | example input | notes |
|---|---|---|
| SRT | `"srt://host:9000"` | caller mode by default; for a listener add `?mode=listener`. Options such as `latency` or `passphrase` go in the URL. Needs FFmpeg built with libsrt (the Gyan and BtbN Windows builds are) |
| RTMP / RTMPS | `rtmp://host/app/key` | pulls from an RTMP server. To let an encoder push straight to this machine instead, listen: `rtmp://0.0.0.0:1935/live/key --input_args="-listen 1"` |
| MPEG-TS over UDP / RTP, incl. multicast | `"udp://239.1.1.1:5000?overrun_nonfatal=1&fifo_size=50000000"` | add `localaddr=<this host's IP>` to pick the network interface that joins the group, `sources=` for SSM. The FIFO options keep bursts from being dropped. `rtp://...` works the same way |
| HLS | `https://host/live/master.m3u8` | with a master playlist only the rendition chosen by `--map` is downloaded; a variant playlist URL works too. The first segments arrive faster than real time (shown as `backlog`) |
| MPEG-DASH (live) | `https://host/live/manifest.mpd` | **partly supported** - see below |
| RTSP, files, devices | `rtsp://cam/stream --input_args="-rtsp_transport tcp"` | anything else FFmpeg's `-i` opens (dshow/v4l2 devices usually need `--reencode`) |

**Live MPEG-DASH.** FFmpeg's DASH demuxer reads the segments a live MPD
lists, but at the live edge it requests the next one before it is
published, fails 100 times without waiting and gives up ("Reached max
consecutive fragment open failures"), with both `SegmentTimeline` and
`$Number$` manifests. `uvq_live.py` then reconnects after
`--reconnect_delay`, so you get a burst of segments every few seconds - scored
samples of the stream, not continuous coverage. If the packager also offers
HLS for the same stream (most do, often with the same CMAF segments), use the
HLS URL instead.

To pick one rendition of a multi-bitrate HLS/DASH stream, use `--map`, e.g.
`--map 0:p:2:v:0` for the video of the third program. Run
`ffprobe <url>` to list the programs.

| option | default | |
|---|---|---|
| `input` | (required) | live URL or device, anything FFmpeg's `-i` accepts |
| `--engine FILE` / `--onnx FILE` | | score on the GPU with TensorRT / on the CPU with ONNX Runtime (one is required) |
| `--segment_seconds S` | `10` | segment length |
| `--segment_dir DIR` | `uvq_live_segments` | where segments are written |
| `--keep_duration T` | keep all | delete segments older than T, e.g. `90s`, `30m`, `2h`, `7d` (plain number = seconds); `0` deletes each one after scoring. Age is counted from when FFmpeg closed the segment; segments still waiting for scoring are never deleted, and leftovers of earlier runs in the same directory are cleaned up too |
| `--map SPEC` | `0:v:0` | FFmpeg `-map` for the video to record, e.g. one rendition of an HLS/DASH stream |
| `--reencode` | off | re-encode with libx264 instead of stream copy (see below) |
| `--reencode_crf N` | `12` | x264 CRF for `--reencode` |
| `--input_args "..."` | none | extra FFmpeg input options, e.g. `-rtsp_transport tcp`. Write it as `--input_args="-re"`: a separate value starting with `-` is taken for an option |
| `--max_segments N` | `0` (forever) | stop after scoring N segments |
| `--reconnect_delay S` | `5` | restart FFmpeg this long after the input drops; negative = exit instead |
| `--max_backlog N` | `0` (never skip) | if more than N segments wait for scoring, skip the oldest so scores stay current |
| `--transpose auto\|on\|off` | `auto` | `auto` transposes portrait video, like `.txt` input to `uvq_trt_inference.py` |
| `--output FILE` | none | JSON-lines log, one line per scored segment |
| `--fps`, `--batch_size`, `--threads`, `--hwaccel`, `--uvq_dir`, `--ffmpeg_path`, `--ffprobe_path` | | as for `uvq_trt_inference.py` |

Notes:

- **Stream copy vs. `--reencode`.** By default the video is stream-copied, so
  what is scored is exactly what was received. Cuts can then only happen on
  keyframes, so segments are about `--segment_seconds` rounded up to the
  next keyframe (e.g. 6.2 s for 5 s with a 2.5 s GOP). `--reencode` gives
  exact lengths and handles inputs MP4 can't hold (raw capture devices,
  unusual codecs), at the cost of a little extra coding loss (CRF 12).
- Only the first video stream is recorded; audio and subtitles are dropped.
- Scoring must keep up with real time, or segments queue up (shown as
  `backlog`, with a warning when one segment takes longer to score than it
  lasts). With the default 1 frame per second this is easy: a 6 s 1440p
  segment scores in about 1 s on an RTX 4060 and in about 2.5 s on a
  Ryzen 7 7700 with ONNX Runtime.

## Running on the CPU with ONNX Runtime

Pass the ONNX file from step 1 with `--onnx` instead of an engine; no GPU,
CUDA or TensorRT needed, and step 2 is skipped:

```bash
python3 uvq_trt/uvq_trt_inference.py video.mp4 --onnx uvq_trt/uvq1p5.onnx --compare_pytorch --output_all_stats
```

`--threads N` limits the CPU threads; all other options except `--hwaccel cuda`
work as above.
With `--compare_pytorch` the output includes `pytorch_uvq1p5_score` (the
original UVQ model on the same frames) and `max_abs_frame_diff`, which
should be around 1e-6.

For example, UVQ's sample clip
[Gaming_1080P-0ce6_orig.mp4](https://storage.googleapis.com/ugc-dataset/vp9_compressed_videos/Gaming_1080P-0ce6_orig.mp4)
scores `3.8483` both this way and with UVQ's own `uvq_inference.py` at the
pinned revision (UVQ's README still lists an older `3.8804`). Small
differences are expected with other FFmpeg versions, since FFmpeg does the
frame sampling and rescaling.

## Windows, CPU only

Needs Python 3.10-3.13 (not 3.14: there are no Windows wheels of
`torch==2.8.0` for it), Git and FFmpeg, e.g. with
`winget install Python.Python.3.13`, `winget install Git.Git` and
`winget install Gyan.FFmpeg`, then reopen PowerShell. In PowerShell, from the
root of this repository:

```powershell
git clone https://github.com/google/uvq.git uvq_trt\uvq
git -C uvq_trt\uvq checkout c204ecc04f093e90c941cb126c7ac3d5beca0414
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r uvq_trt\requirements.txt
python uvq_trt\export_onnx.py -o uvq_trt\uvq1p5.onnx
python uvq_trt\uvq_trt_inference.py video.mp4 --onnx uvq_trt\uvq1p5.onnx --compare_pytorch --output_all_stats
```

If `Activate.ps1` is blocked ("running scripts is disabled"), run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.

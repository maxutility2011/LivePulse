# Processor

The processor follows a live **HLS variant playlist** - typically the
[ingestor](../ingestor/README.md)'s output - downloads it segment after
segment, prepends the init segment to every fMP4 media segment so it decodes
on its own, and scores the frames with Google's
[UVQ 1.5](https://github.com/google/uvq) no-reference quality model. A
**real-time web UI** charts every frame's score as soon as it is computed.
It runs as a Docker service.

```bash
docker build -t livepulse-processor processor/
docker run -d --gpus all -p 8000:8000 -v lp-engines:/var/cache/livepulse livepulse-processor \
    --input_url http://<ingestor-host>:8080/cam1/index.m3u8
# open http://<host>:8000/
```

## GPU or CPU

`--device auto` (the default) uses the GPU whenever one is usable - CUDA
visible and TensorRT installed - and otherwise falls back to the CPU:

| | GPU | CPU |
|---|---|---|
| runtime | TensorRT (FP32 engine) | ONNX Runtime |
| default `--fps` | **5** | **1** |
| default batch | 8 frames | 1 frame |
| measured, 6 s 1440p segment | 31 frames in ~1.7 s (RTX 4060) | 6-8 frames in ~2.5 s (Ryzen 7 7700) |

- In Docker the GPU is only visible with `--gpus all` (or a Compose
  `deploy.resources` GPU reservation) and the NVIDIA Container Toolkit on the
  host (built into Docker Desktop with WSL 2 on Windows). Without it the
  container logs `No usable GPU (...); using the CPU`.
- A TensorRT engine is specific to the GPU model and TensorRT version, so the
  first start on a GPU builds one from the ONNX model (a few minutes) and
  caches it in `/var/cache/livepulse`. Mount a volume there
  (`-v lp-engines:/var/cache/livepulse`) to build it only once. The web UI is
  up during the build and says so.
- FP16 engines are not used: UVQ's AggregationNet overflows FP16 and returns
  NaN (see [uvq_trt/README.md](uvq_trt/README.md)).

**What `--fps` means.** UVQ samples `--fps` frames **per second of video**
(with FFmpeg's `fps` filter), so `--fps 5` is every 5th frame of a 25 fps
stream, every 6th of 30 fps and every 10th of 50 fps. Scoring time grows
with it: about 25 ms per frame on an RTX 4060 (after decoding), about 0.35 s
per frame on a Ryzen 7 7700 CPU.

## How segments are scored

1. The playlist is polled and new segments are downloaded in order, starting
   three segments behind the live edge (`--live_edge_segments`). A master
   playlist works too (the highest-bandwidth variant is used).
2. fMP4/CMAF: `init segment + media segment` is written to one file, which
   decodes on its own; TS segments are used as they are.
3. FFmpeg decodes it with UVQ's own pipeline (`fps` sampling, bicubic scale
   to 1920x1080, RGB), and the frames go to TensorRT / ONNX Runtime in
   batches. Unlike UVQ's `uvq_inference.py`, nothing is padded: UVQ rounds a
   video up to whole seconds with black frames, which would drag live scores
   down (and for fMP4 segments that keep stream timestamps, ffprobe reports a
   duration counted from 0, e.g. 12.15 s for a 6.1 s segment). Only frames
   FFmpeg actually produces are scored.
4. Each batch's scores go to the web UI immediately; the segment's summary
   (mean, min) follows when it is done.

If scoring falls behind (more than `--max_backlog` segments waiting, default
3), the oldest waiting segments are skipped so the scores stay live; they are
counted and logged.

Each frame gets a wall-clock time: from `EXT-X-PROGRAM-DATE-TIME` when the
playlist has it, otherwise estimated from when the segment appeared in the
playlist (a live segment is listed right after it ends).

## Web UI

`http://<host>:8000/` (`--http_port`):

- tiles: latest frame score, latest segment mean, mean and minimum over the
  selected window, delay from a segment appearing in the playlist to its
  score, and segment counts (scored / skipped / errors / backlog);
- a live line chart of every frame's score with a crosshair tooltip (time,
  score, segment), a 1 min / 5 min / 15 min / 1 h window, a **Scale 1-5**
  (absolute, with the `--low_score` threshold line) or **Fit to data** (to
  see small changes) axis, and Follow live;
- a table of recent segments with their frame count, mean and minimum.

Scores are pushed with Server-Sent Events as each inference batch finishes
(8 frames on the GPU, every frame on the CPU), so the chart moves within
milliseconds of a score existing. The page has no external dependencies and
follows the system light/dark theme. URL parameters set the initial view,
e.g. `/?window=900&scale=fit` for a wall display.

| endpoint | |
|---|---|
| `GET /` | the page |
| `GET /api/state?window=600` | status + recent frames `[id, time, score, segment seq]` and segment summaries (JSON) |
| `GET /events` | Server-Sent Events stream of new frames, segments and status (resumes with `Last-Event-ID`) |
| `GET /healthz` | liveness |

The browser keeps up to an hour of frames; the processor keeps
`--history_frames` (default 200,000) in memory.

## Output for the monitor

`--output scores.jsonl` appends one JSON line per segment, in the format the
[monitor](../monitor/README.md) reads:

```json
{"stream": "cam1", "segment": "seg_00000004.m4s", "seq": 3, "status": "ok",
 "start": "2026-11-07T20:52:01.120", "finished_at": "2026-11-07T20:52:07.120", "duration": 6.0,
 "scored_at": "2026-11-07T20:52:08.731", "model": "uvq1p5", "device": "gpu",
 "backend": "TensorRT on NVIDIA GeForce RTX 4060", "fps": 5, "uvq1p5_score": 4.6315,
 "per_frame_scores": [...], "frame_times": [...], "scoring_seconds": 1.63, "delay_seconds": 1.8}
```

`status` is `ok`, `skipped` or `error`.

## Options

| option | default | |
|---|---|---|
| `--input_url URL` | (required) | HLS variant playlist (a master playlist works too) |
| `--stream_id NAME` | from the URL (`.../cam1/index.m3u8` -> `cam1`) | name in the UI and output |
| `--device auto\|gpu\|cpu` | `auto` | GPU when usable, else CPU; `gpu` fails if none is usable |
| `--fps N` | 5 GPU / 1 CPU | frames sampled per second of video |
| `--batch_size N` | 8 GPU / 1 CPU | frames per inference call (GPU: up to the engine's max batch, 8) |
| `--threads N` | all cores | ONNX Runtime CPU threads |
| `--transpose auto\|on\|off` | `auto` | transpose portrait video before scoring |
| `--onnx FILE` | `/opt/livepulse/models/uvq1p5.onnx` in the image | UVQ 1.5 ONNX model |
| `--engine FILE` | built and cached | use this TensorRT engine instead |
| `--engine_cache_dir DIR` | `/var/cache/livepulse` in the image | where engines are built and cached |
| `--max_backlog N` | `3` | skip the oldest waiting segments beyond N (`0` = never skip) |
| `--live_edge_segments N` | `3` | start this many segments behind the live edge |
| `--http_port N` / `--http_host` | `8000` / `0.0.0.0` | web UI (`0` = off) |
| `--low_score X` | `3.0` | threshold line in the UI |
| `--output FILE` | none | JSON-lines per segment, for the monitor |
| `--history_frames N` | `200000` | frames kept in memory for the UI |
| `--max_segments N` | `0` | stop after N scored segments (testing) |

## The image

`processor/Dockerfile` installs FFmpeg, PyTorch 2.8 (CUDA 12.8 build),
TensorRT 10 and ONNX Runtime, clones Google's UVQ at the pinned revision and
exports the ONNX model at build time (verified against PyTorch UVQ by
`uvq_trt/export_onnx.py`). The CUDA build also runs on CPU-only hosts; for a
much smaller CPU-only image:

```bash
docker build -t livepulse-processor:cpu \
    --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu --build-arg WITH_TENSORRT=0 processor/
```

## Without Docker

```bash
pip install -r processor/uvq_trt/requirements.txt   # plus tensorrt-cu12 and CUDA torch for the GPU
processor/uvq_trt/fetch_uvq.sh && python3 processor/uvq_trt/export_onnx.py -o processor/uvq_trt/uvq1p5.onnx
python3 processor/lp_processor.py --input_url http://localhost:8080/cam1/index.m3u8
```

Engines are then cached in `processor/uvq_trt/engines/`.

`uvq_trt/` also has the stand-alone tools: `uvq_trt_inference.py` scores
video files, `export_onnx.py` / `build_engine.py` make the models - see
[uvq_trt/README.md](uvq_trt/README.md).

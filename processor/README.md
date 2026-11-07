# Processor

`lp_processor.py` follows the `segments.jsonl` manifest of an
[ingestor](../ingestor/README.md) and scores every new segment with Google's
[UVQ 1.5](https://github.com/google/uvq) no-reference quality model, on an
NVIDIA GPU with TensorRT (`--engine`) or on the CPU with ONNX Runtime
(`--onnx`). The model is loaded once and stays loaded. Results go to
`<segments_dir>/scores.jsonl`, one line per segment, which the
[monitor](../monitor/README.md) follows.

```bash
python3 processor/lp_processor.py live/cam1 --engine processor/uvq_trt/uvq1p5_fp32.engine
python3 processor/lp_processor.py live/cam1 --onnx processor/uvq_trt/uvq1p5.onnx --max_backlog 6
```

The UVQ model code, the ONNX export and the TensorRT engine build live in
[`uvq_trt/`](uvq_trt/README.md): set it up there first (fetch UVQ, export the
ONNX file, optionally build an engine). `uvq_trt/uvq_trt_inference.py` also
scores single video files.

## Output

```json
{"stream": "cam1", "segment": "seg_20261107-125327-057_00000.mp4", "status": "ok",
 "finished_at": "2026-11-07T12:53:32", "duration": 6.16, "scored_at": "2026-11-07T12:53:33",
 "model": "uvq1p5", "backend": "tensorrt-gpu", "transpose": false, "scoring_seconds": 0.8,
 "uvq1p5_score": 4.6234, "per_frame_scores": [4.59, 4.63, ...], "frame_indices": [0, 25, ...]}
```

`status` is `ok`, `error` (with `error`: the segment couldn't be read, or
was already deleted by the ingestor's `--keep_duration`) or `skipped`
(dropped by `--max_backlog`). Segments already in the scores file are never
scored again, so the processor can be stopped and restarted at any time and
picks up where it left off.

## Options

| option | default | |
|---|---|---|
| `segments_dir` | (required) | the ingestor's `--out_dir` |
| `--engine FILE` / `--onnx FILE` | | score on the GPU with TensorRT / on the CPU with ONNX Runtime (one is required) |
| `--output FILE` | `<segments_dir>/scores.jsonl` | scores file |
| `--max_backlog N` | `0` (never skip) | if more than N segments wait for scoring, skip the oldest so scores stay current |
| `--max_segments N` | `0` (forever) | stop after scoring N segments |
| `--poll_interval S` | `0.5` | seconds between manifest checks |
| `--transpose auto\|on\|off` | `auto` | `auto` transposes portrait video before scoring |
| `--fps`, `--batch_size`, `--threads`, `--hwaccel`, `--uvq_dir`, `--ffmpeg_path`, `--ffprobe_path` | | as for [`uvq_trt_inference.py`](uvq_trt/README.md#3-run-inference) |

Scoring must keep up with real time, or segments queue up (logged as
`backlog`, with a warning when a segment takes longer to score than it
lasts). With the default 1 frame per second this is easy: a 6 s 1440p
segment scores in about 1 s on an RTX 4060 and in about 2.5 s on a
Ryzen 7 7700 with ONNX Runtime. One processor can follow one stream; run one
per stream.

# LivePulse

Live video monitoring: ingest live streams, assess their quality with
Google's [UVQ 1.5](https://github.com/google/uvq) no-reference model, and
track, alert on and export the results.

```
 live stream ──> ingestor ──> fMP4 segments + segments.jsonl
                                   │
                              processor (UVQ on GPU/CPU) ──> scores.jsonl
                                                                 │
                                   monitor ──> alerts, reports, Prometheus/Grafana, Datadog
```

| folder | | |
|---|---|---|
| [`ingestor/`](ingestor/README.md) | `lp_ingestor.py` | reads a live stream (SRT, RTMP, UDP/multicast MPEG-TS, HLS, RTSP, ...) and cuts it into fragmented MP4 segments |
| [`processor/`](processor/README.md) | `lp_processor.py` | scores each segment with UVQ 1.5 on TensorRT (GPU) or ONNX Runtime (CPU); the UVQ export/engine tooling is in [`processor/uvq_trt/`](processor/uvq_trt/README.md) |
| [`monitor/`](monitor/README.md) | `lp_monitor.py` | rolling statistics, low-quality and stale alerts, reports, Prometheus and DogStatsD export |

The three parts are separate processes that only share files: the ingestor
appends to `segments.jsonl`, the processor follows it and appends to
`scores.jsonl`, and the monitor follows that. Each can be restarted on its
own (the processor resumes where it left off), and the processor can run on
a GPU machine that sees the segment folder over a shared filesystem.

## Quick start

Set up UVQ once (see [processor/uvq_trt/README.md](processor/uvq_trt/README.md),
including Windows notes):

```bash
processor/uvq_trt/fetch_uvq.sh
pip install -r processor/uvq_trt/requirements.txt
python3 processor/uvq_trt/export_onnx.py -o processor/uvq_trt/uvq1p5.onnx
# optional, NVIDIA GPU: python3 processor/uvq_trt/build_engine.py processor/uvq_trt/uvq1p5.onnx
```

Then run the three parts, e.g. in three terminals:

```bash
python3 ingestor/lp_ingestor.py "srt://host:9000" --stream_id cam1 --out_dir live/cam1 --keep_duration 1h
python3 processor/lp_processor.py live/cam1 --onnx processor/uvq_trt/uvq1p5.onnx
python3 monitor/lp_monitor.py "live/*/scores.jsonl" --prometheus_port 9108
```

To try it without a live source, feed a file in real time:
`python3 ingestor/lp_ingestor.py video.mp4 --input_args="-re -stream_loop -1" --out_dir live/test`.

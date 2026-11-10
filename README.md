# LivePulse

Live video monitoring: ingest live streams, assess their quality with
Google's [UVQ 1.5](https://github.com/google/uvq) no-reference model, and
track, alert on and export the results. Each part runs as an independent
microservice in its own container.

```
 live stream ──> ingestor (FFmpeg + Nginx) ──> HLS, fMP4 segments (bucket or Nginx)
                                                   │
                       processor (UVQ on GPU/CPU) <┘ ──> real-time web UI (per-frame chart)
                                   │
                                   └──> scores.jsonl ──> monitor ──> alerts, Prometheus/Grafana, Datadog
```

| folder | | status |
|---|---|---|
| [`ingestor/`](ingestor/README.md) | `lp_ingestor.py` + Dockerfile | **containerized.** One live stream per container (RTMP/SRT push, SRT pull, UDP/multicast MPEG-TS, HLS, DASH) -> HLS with fMP4 segments cut on GOP boundaries (or passed through), published to a GCS/S3 bucket or the container's Nginx |
| [`processor/`](processor/README.md) | `lp_processor.py` + Dockerfile | **containerized.** Follows an HLS variant playlist, scores every segment's frames with UVQ 1.5 on the GPU (TensorRT, 5 frames/s) or CPU (ONNX Runtime, 1 frame/s), and charts them live in a web UI; writes per-segment scores for the monitor |
| [`monitor/`](monitor/README.md) | `lp_monitor.py` | rolling statistics, low-quality and stale alerts, reports, Prometheus and DogStatsD export from the processor's scores. *To be containerized* |

## Quick start (one host)

```bash
docker compose up -d --build
# send SRT to srt://<host>:9000
# UVQ chart:  http://<host>:8000/
# HLS:        http://<host>:8080/cam1/index.m3u8
```

[`docker-compose.yml`](docker-compose.yml) runs one ingestor and one
processor (with the GPU when the host has one). The services only talk over
HTTP (the processor reads the ingestor's playlist), so they can equally run
on different hosts or clusters - e.g. ingestors at the edge publishing to a
bucket, and processors on GPU nodes reading from it.

See [ingestor/README.md](ingestor/README.md) and
[processor/README.md](processor/README.md) for all options.

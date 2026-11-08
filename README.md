# LivePulse

Live video monitoring: ingest live streams, assess their quality with
Google's [UVQ 1.5](https://github.com/google/uvq) no-reference model, and
track, alert on and export the results. The three parts are meant to run as
independent microservices, each in its own container.

```
 live stream ──> ingestor (Docker: FFmpeg + Nginx) ──> HLS (fMP4) in a GCS/S3 bucket or Nginx
                                                          │
                                processor (UVQ on GPU/CPU) ──> scores
                                                                  │
                                       monitor ──> alerts, reports, Prometheus/Grafana, Datadog
```

| folder | | status |
|---|---|---|
| [`ingestor/`](ingestor/README.md) | `lp_ingestor.py` + Dockerfile | **containerized.** One live stream per container (RTMP/SRT push, SRT pull, UDP/multicast MPEG-TS, HLS, DASH) -> HLS with fMP4 segments cut on GOP boundaries (or passed through), published to a bucket or the container's Nginx |
| [`processor/`](processor/README.md) | `lp_processor.py` | scores segments with UVQ 1.5 on TensorRT (GPU) or ONNX Runtime (CPU); the UVQ export/engine tooling is in [`processor/uvq_trt/`](processor/uvq_trt/README.md). *Still reads the previous ingestor's `segments.jsonl`; to be redesigned to consume the HLS output and containerized* |
| [`monitor/`](monitor/README.md) | `lp_monitor.py` | rolling statistics, low-quality and stale alerts, reports, Prometheus and DogStatsD export from the processor's `scores.jsonl`. *To be containerized* |

## Ingestor quick start

```bash
docker build -t livepulse-ingestor ingestor/
docker run -d -p 9000:9000/udp -p 8080:80 livepulse-ingestor \
    --input_url srt://0.0.0.0 --port 9000 --stream_id cam1
# send SRT to srt://<host>:9000, play http://<host>:8080/cam1/index.m3u8
```

See [ingestor/README.md](ingestor/README.md) for all inputs, cloud upload and
options.

## UVQ setup (processor)

See [processor/uvq_trt/README.md](processor/uvq_trt/README.md), including
Windows notes:

```bash
processor/uvq_trt/fetch_uvq.sh
pip install -r processor/uvq_trt/requirements.txt
python3 processor/uvq_trt/export_onnx.py -o processor/uvq_trt/uvq1p5.onnx
# optional, NVIDIA GPU: python3 processor/uvq_trt/build_engine.py processor/uvq_trt/uvq1p5.onnx
```

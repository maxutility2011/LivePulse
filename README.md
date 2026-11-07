# LivePulse

Live video monitoring.

## Components

| folder | |
|---|---|
| [`uvq_trt/`](uvq_trt/README.md) | Google's [UVQ 1.5](https://github.com/google/uvq) no-reference video quality model on TensorRT (GPU) or ONNX Runtime (CPU): score video files with `uvq_trt_inference.py`, or live streams (SRT, RTMP, UDP/multicast MPEG-TS, HLS, ...) continuously with `uvq_live.py` |

## Quick start

```bash
uvq_trt/fetch_uvq.sh                      # UVQ model code + checkpoints
pip install -r uvq_trt/requirements.txt
python3 uvq_trt/export_onnx.py -o uvq_trt/uvq1p5.onnx
python3 uvq_trt/uvq_live.py "srt://host:9000" --onnx uvq_trt/uvq1p5.onnx --output live.jsonl
```

See [uvq_trt/README.md](uvq_trt/README.md) for TensorRT engines, Windows
setup and all options.

# LivePulse processor: UVQ 1.5 scores for a live HLS stream + real-time web UI.
#
#   docker build -t livepulse-processor processor/
#   docker run -d --gpus all -p 8000:8000 -v lp-engines:/var/cache/livepulse livepulse-processor \
#       --input_url http://<ingestor>/cam1/index.m3u8
#   -> http://localhost:8000/
#
# Without --gpus all (or without an NVIDIA GPU) it scores on the CPU. For a
# smaller CPU-only image:
#   docker build -t livepulse-processor:cpu \
#       --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu --build-arg WITH_TENSORRT=0 processor/
FROM python:3.12-slim-trixie

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg git tini ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# PyTorch: CUDA 12.8 build by default (works on CPU-only hosts too).
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cu128
RUN pip install --no-cache-dir torch==2.8.0 torchvision==0.23.0 --index-url ${TORCH_INDEX_URL}
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
# TensorRT 10 (not 11: build_engine.py is tested with 10.x).
ARG WITH_TENSORRT=1
RUN if [ "$WITH_TENSORRT" = "1" ]; then pip install --no-cache-dir "tensorrt-cu12>=10.8,<11"; fi

WORKDIR /app
COPY . /app

# Google's UVQ model code + checkpoints at the pinned revision, exported once to
# ONNX (verified against PyTorch UVQ by export_onnx.py). TensorRT engines are
# GPU-specific, so they are built on first start and cached in the volume.
ARG UVQ_REV=c204ecc04f093e90c941cb126c7ac3d5beca0414
RUN git clone --quiet https://github.com/google/uvq.git /app/uvq_trt/uvq \
 && git -C /app/uvq_trt/uvq checkout --quiet ${UVQ_REV} \
 && rm -rf /app/uvq_trt/uvq/.git \
 && mkdir -p /opt/livepulse/models /var/cache/livepulse \
 && python /app/uvq_trt/export_onnx.py -o /opt/livepulse/models/uvq1p5.onnx

ENV PYTHONUNBUFFERED=1 \
    LP_ONNX=/opt/livepulse/models/uvq1p5.onnx \
    LP_ENGINE_CACHE=/var/cache/livepulse
VOLUME /var/cache/livepulse
EXPOSE 8000

ENTRYPOINT ["tini", "--", "python", "/app/lp_processor.py"]
CMD ["--help"]

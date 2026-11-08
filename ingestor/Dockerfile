# LivePulse ingestor: FFmpeg + Nginx + lp_ingestor.py
#
#   docker build -t livepulse-ingestor ingestor/
#   docker run -p 9000:9000/udp -p 8080:80 livepulse-ingestor \
#       --input_url srt://0.0.0.0 --port 9000 --stream_id cam1
#   -> http://localhost:8080/cam1/index.m3u8
FROM python:3.12-slim-trixie

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg nginx tini ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && rm -f /etc/nginx/sites-enabled/default

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY lp_ingestor.py continuous.py pull.py hls_output.py ./
COPY nginx.conf /etc/nginx/conf.d/livepulse.conf
COPY entrypoint.sh /usr/local/bin/lp-entrypoint
# Tolerate CRLF checkouts on Windows.
RUN sed -i 's/\r$//' /usr/local/bin/lp-entrypoint && chmod +x /usr/local/bin/lp-entrypoint

ENV PYTHONUNBUFFERED=1
# Nginx (HLS output). The ingest port is chosen at run time with --port and
# published with -p; use /udp for SRT, UDP and RTP. Uploads to GCS/S3 are
# outgoing HTTPS (443) and need no published port.
ARG INGEST_PORT=1935
EXPOSE 80 ${INGEST_PORT}/tcp ${INGEST_PORT}/udp

ENTRYPOINT ["tini", "--", "lp-entrypoint"]
CMD ["--help"]

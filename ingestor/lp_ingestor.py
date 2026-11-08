"""LivePulse ingestor: turns one live stream into an HLS stream with fMP4 segments.

Inputs (one stream per ingestor):
  RTMP push, SRT push, MPEG-TS over UDP/RTP (incl. multicast)
      the ingestor listens on --port
  SRT pull, HLS, DASH
      the ingestor pulls from --port on the remote host

Continuous inputs (RTMP, SRT, MPEG-TS) are stream-copied - never re-encoded -
and cut on GOP boundaries: each segment holds the fewest whole GOPs that
reach --segment_seconds (5 s with a 2 s GOP -> 3 GOPs = 6 s). HLS inputs
are passed through segment by segment (TS, CMAF or fMP4), and DASH inputs
are passed through and republished as HLS.

The output - a media playlist, init segment(s) and media segments - goes to
the cloud bucket given by --cloud_upload_url (gs://... or s3://...), or else
to <--nginx_webroot>/<--stream_id>/ to be served by Nginx.

Examples:
  # RTMP push to rtmp://<host>:1935/live/cam1, served by Nginx
  python ingestor/lp_ingestor.py --input_url rtmp://0.0.0.0/live/cam1 --port 1935 --stream_id cam1
  # SRT push (listener) -> GCS
  python ingestor/lp_ingestor.py --input_url srt://0.0.0.0 --port 9000 --cloud_upload_url gs://my-bucket/live
  # SRT pull (caller) from an encoder
  python ingestor/lp_ingestor.py --input_url "srt://encoder.example.com?latency=200000" --port 9000 --input_mode pull
  # Multicast MPEG-TS
  python ingestor/lp_ingestor.py --input_url "udp://239.1.1.1?localaddr=10.0.0.5" --port 5000
  # HLS or DASH pull -> S3
  python ingestor/lp_ingestor.py --input_url https://cdn.example.com/live/index.m3u8 --port 443 --cloud_upload_url s3://bucket/live
"""

import argparse
import logging
import os
import signal
import sys
import threading
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from continuous import ContinuousIngest  # noqa: E402
from hls_output import HlsOutput, make_publisher  # noqa: E402
from pull import DashPull, HlsPull, http_get  # noqa: E402

log = logging.getLogger("ingestor")

DEFAULT_MODE = {"rtmp": "listen", "rtmps": "listen", "srt": "listen", "udp": "listen", "rtp": "listen",
                "http": "pull", "https": "pull"}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n\n".join(__doc__.split("\n\n")[1:]),
    )
    src = parser.add_argument_group("input")
    src.add_argument("--input_url", required=True,
                     help="rtmp://, srt://, udp://, rtp:// (host = bind/multicast address when listening, "
                          "remote host when pulling) or http(s):// HLS/DASH URL")
    src.add_argument("--port", type=int, required=True,
                     help="Local port to listen on (RTMP/SRT push, UDP/RTP) or remote port to pull from "
                          "(SRT pull, HLS/DASH)")
    src.add_argument("--input_mode", choices=["listen", "pull"], default=None,
                     help="listen (push inputs) or pull; default: listen for rtmp/srt/udp/rtp, pull for http(s)")
    src.add_argument("--input_format", choices=["auto", "hls", "dash"], default="auto",
                     help="For http(s) inputs; auto = by .m3u8/.mpd extension or content")
    src.add_argument("--input_args", default="", help='Extra FFmpeg input options, e.g. --input_args="-re"')
    src.add_argument("--stall_timeout", type=float, default=15.0,
                     help="Seconds without data before an SRT/UDP/RTP input is reopened (0 = never; default 15)")
    src.add_argument("--reconnect_delay", type=float, default=3.0,
                     help="Seconds before reopening a dropped continuous input; negative = exit instead (default 3)")

    seg = parser.add_argument_group("segmenting")
    seg.add_argument("--segment_seconds", type=float, default=6.0,
                     help="Minimum segment length; continuous inputs are cut after the fewest whole GOPs reaching it "
                          "(default 6)")
    seg.add_argument("--audio", choices=["copy", "aac", "none"], default="copy",
                     help="Continuous inputs: copy the first audio stream (AAC expected), re-encode it to AAC, "
                          "or drop it (default copy). Video is never re-encoded")
    seg.add_argument("--audio_bitrate", default="128k", help="Bitrate for --audio aac (default 128k)")

    out = parser.add_argument_group("output")
    out.add_argument("--stream_id", default="live", help="Output folder / object prefix for this stream (default live)")
    out.add_argument("--cloud_upload_url", default="",
                     help="gs://bucket[/prefix] or s3://bucket[/prefix]; if not given, write to --nginx_webroot")
    out.add_argument("--nginx_webroot", default="/var/www/html", help="Nginx webroot (default /var/www/html)")
    out.add_argument("--playlist_name", default="index.m3u8", help="Media playlist name (default index.m3u8)")
    out.add_argument("--hls_list_size", type=int, default=10,
                     help="Segments in the live playlist; older ones are deleted (0 = keep all, EVENT playlist)")
    out.add_argument("--staging_dir", default=os.path.join(os.sep, "tmp", "lp_storage"),
                     help="Scratch space for FFmpeg's per-GOP fragments (default /tmp/lp_storage)")

    misc = parser.add_argument_group("misc")
    misc.add_argument("--max_segments", type=int, default=0, help="Stop after publishing N segments (0 = run forever)")
    misc.add_argument("--no_endlist", action="store_true", help="Don't mark the playlist as ended when stopping")
    misc.add_argument("--ffmpeg_path", default="ffmpeg")
    misc.add_argument("--log_level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)
    if args.segment_seconds <= 0:
        parser.error("--segment_seconds must be positive")
    if not 0 < args.port < 65536:
        parser.error("--port must be 1-65535")
    return args


def with_port(url, port):
    """Puts --port into a pull URL, or checks that the URL's own port agrees."""
    if url.port is not None and url.port != port:
        raise SystemExit(f"--port {port} disagrees with the port in --input_url ({url.port})")
    host = url.hostname or ""
    if ":" in host:
        host = f"[{host}]"  # IPv6
    userinfo = url.netloc.rsplit("@", 1)[0] + "@" if "@" in url.netloc else ""
    return url._replace(netloc=f"{userinfo}{host}:{port}")


def add_query(query, **defaults):
    params = urllib.parse.parse_qsl(query, keep_blank_values=True)
    present = {k for k, _ in params}
    params += [(k, str(v)) for k, v in defaults.items() if v is not None and k not in present]
    return urllib.parse.urlencode(params, safe=":/")


def plan_input(args):
    """Returns ("continuous", ffmpeg_input_args, description) or ("hls"/"dash", url, description)."""
    url = urllib.parse.urlparse(args.input_url)
    scheme = url.scheme.lower()
    if scheme not in DEFAULT_MODE:
        raise SystemExit(f"Unsupported input scheme '{scheme}://' (use rtmp, srt, udp, rtp, http or https)")
    mode = args.input_mode or DEFAULT_MODE[scheme]
    port = args.port
    stall_us = int(args.stall_timeout * 1e6) if args.stall_timeout > 0 else None

    if scheme in ("http", "https"):
        if mode != "pull":
            raise SystemExit("HLS/DASH inputs can only be pulled")
        full = urllib.parse.urlunparse(with_port(url, port))
        fmt = args.input_format
        if fmt == "auto":
            path = url.path.lower()
            if path.endswith(".mpd"):
                fmt = "dash"
            elif path.endswith(".m3u8"):
                fmt = "hls"
            else:
                body = http_get(full)[0][:2048]
                fmt = "dash" if b"<MPD" in body else "hls"
        return fmt, full, f"{fmt.upper()} pull from {full}"

    if scheme in ("rtmp", "rtmps"):
        path = url.path if url.path not in ("", "/") else "/live/stream"
        if mode == "listen":
            u = f"{scheme}://0.0.0.0:{port}{path}"
            return "continuous", ["-listen", "1", "-i", u], f"RTMP push on port {port} ({path})"
        u = urllib.parse.urlunparse(with_port(url, port))
        return "continuous", ["-i", u], f"RTMP pull from {u}"

    if scheme == "srt":
        if mode == "listen":
            q = add_query(url.query, mode="listener", timeout=stall_us)
            u = f"srt://{url.hostname or '0.0.0.0'}:{port}?{q}"
            return "continuous", ["-i", u], f"SRT push (listener) on port {port}"
        q = add_query(url.query, mode="caller", timeout=stall_us)
        u = urllib.parse.urlunparse(with_port(url, port)._replace(query=q))
        return "continuous", ["-i", u], f"SRT pull (caller) from {url.hostname}:{port}"

    # udp / rtp: MPEG-TS, unicast or multicast; always received (listen)
    if mode != "listen":
        raise SystemExit("UDP/RTP inputs are received on a local port; use --input_mode listen")
    host = url.hostname or "0.0.0.0"
    extra = {"overrun_nonfatal": 1, "fifo_size": 50000000} if scheme == "udp" else {}
    q = add_query(url.query, timeout=stall_us, **extra)
    u = f"{scheme}://{host}:{port}?{q}"
    kind = "multicast" if host.split(".")[0].isdigit() and 224 <= int(host.split(".")[0]) <= 239 else "unicast"
    return "continuous", ["-i", u], f"MPEG-TS over {scheme.upper()} ({kind} {host}:{port})"


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S", stream=sys.stdout)
    kind, spec, description = plan_input(args)
    publisher = make_publisher(args.cloud_upload_url, args.nginx_webroot, args.stream_id)
    output = HlsOutput(publisher, args.playlist_name, args.hls_list_size)
    log.info("Input:  %s", description)
    log.info("Output: %s/%s (%gs segments, %s)", publisher.description.rstrip("/"), args.playlist_name,
             args.segment_seconds, f"window {args.hls_list_size}" if args.hls_list_size else "keep all")

    if kind == "continuous":
        ingest = ContinuousIngest(args, spec, output)
    elif kind == "hls":
        ingest = HlsPull(args, spec, output)
    else:
        ingest = DashPull(args, spec, output)

    # docker stop sends SIGTERM: treat it like Ctrl+C.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    result = {}
    worker = threading.Thread(target=lambda: result.setdefault("ended", ingest.run()), name="ingest", daemon=True)
    worker.start()
    try:
        while worker.is_alive():
            worker.join(0.5)
            if args.max_segments and output.published >= args.max_segments:
                log.info("Published %d segments (--max_segments)", output.published)
                break
    except KeyboardInterrupt:
        log.info("Stopping...")
    ingest.stop()
    worker.join(20)
    output.finish(endlist=not args.no_endlist)
    log.info("Done: %d segments published", output.published)
    return 0


if __name__ == "__main__":
    sys.exit(main())

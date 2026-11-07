# Ingestor

`lp_ingestor.py` reads a live stream with FFmpeg and cuts it into
fixed-length **fragmented MP4** (fMP4) segments. Each finished segment is
announced with one line in `<out_dir>/segments.jsonl`, which the
[processor](../processor/README.md) follows:

```json
{"stream": "cam1", "segment": "seg_20261107-125327-057_00000.mp4", "duration": 6.16, "finished_at": "2026-11-07T12:53:32"}
```

A line is written only after FFmpeg has closed the segment, so every listed
file is complete. Segments are named `seg_<FFmpeg start time>_<counter>.mp4`;
the counter restarts when FFmpeg reconnects.

```bash
python3 ingestor/lp_ingestor.py "srt://host:9000" --stream_id cam1 --out_dir live/cam1
python3 ingestor/lp_ingestor.py rtmp://host/live/key --out_dir live/key --keep_duration 2h
# simulate a live stream from a file
python3 ingestor/lp_ingestor.py video.mp4 --input_args="-re -stream_loop -1" --out_dir live/test
```

Run one ingestor per stream, each with its own `--out_dir`. Stop with Ctrl+C
(FFmpeg is asked to finish the open segment cleanly) or `--max_segments`.

## Options

| option | default | |
|---|---|---|
| `input` | (required) | live URL or device, anything FFmpeg's `-i` accepts |
| `--out_dir DIR` | `live` | where segments and `segments.jsonl` are written |
| `--stream_id NAME` | `out_dir` name | stream name in all records, metrics and alerts |
| `--segment_seconds S` | `10` | segment length |
| `--keep_duration T` | keep all | delete segments older than T, e.g. `90s`, `30m`, `2h`, `7d` (plain number = seconds). Age counts from when FFmpeg closed the segment; leftovers of earlier runs are cleaned up too. Must be longer than the processor may fall behind, or it finds segments already deleted (recorded as `status: error`) |
| `--map SPEC` | `0:v:0` | FFmpeg `-map` for the video to record, e.g. one rendition of an HLS/DASH stream |
| `--reencode` | off | re-encode with libx264 instead of stream copy (see below) |
| `--reencode_crf N` | `12` | x264 CRF for `--reencode` |
| `--input_args "..."` | none | extra FFmpeg input options, e.g. `-rtsp_transport tcp`. Write it as `--input_args="-re"`: a separate value starting with `-` is taken for an option |
| `--reconnect_delay S` | `5` | restart FFmpeg this long after the input drops; negative = exit instead |
| `--max_segments N` | `0` (forever) | stop after N segments |
| `--ffmpeg_path PATH` | `ffmpeg` | FFmpeg executable |

- **Stream copy vs. `--reencode`.** By default the video is stream-copied, so
  the segments hold exactly what was received. Cuts can then only happen on
  keyframes, so segments are about `--segment_seconds` rounded up to the next
  keyframe (e.g. 6.2 s for 5 s with a 2.5 s GOP). `--reencode` gives exact
  lengths and handles inputs MP4 can't hold (raw capture devices, unusual
  codecs), at the cost of a little extra coding loss (CRF 12).
- Only one video stream is recorded; audio, subtitles and data are dropped.

## Input formats

All of these were tested end to end with FFmpeg 9 (a local FFmpeg sender
feeding the ingestor), except live MPEG-DASH, see below:

| format | example input | notes |
|---|---|---|
| SRT | `"srt://host:9000"` | caller mode by default; for a listener add `?mode=listener`. Options such as `latency` or `passphrase` go in the URL. Needs FFmpeg built with libsrt (the Gyan and BtbN Windows builds are) |
| RTMP / RTMPS | `rtmp://host/app/key` | pulls from an RTMP server. To let an encoder push straight to this machine instead, listen: `rtmp://0.0.0.0:1935/live/key --input_args="-listen 1"` |
| MPEG-TS over UDP / RTP, incl. multicast | `"udp://239.1.1.1:5000?overrun_nonfatal=1&fifo_size=50000000"` | add `localaddr=<this host's IP>` to pick the network interface that joins the group, `sources=` for SSM. The FIFO options keep bursts from being dropped. `rtp://...` works the same way |
| HLS | `https://host/live/master.m3u8` | with a master playlist only the rendition chosen by `--map` is downloaded; a variant playlist URL works too. The first segments arrive faster than real time |
| MPEG-DASH (live) | `https://host/live/manifest.mpd` | **partly supported**, see below |
| RTSP, files, devices | `rtsp://cam/stream --input_args="-rtsp_transport tcp"` | anything else FFmpeg's `-i` opens (dshow/v4l2 devices usually need `--reencode`) |

**Live MPEG-DASH.** FFmpeg's DASH demuxer reads the segments a live MPD
lists, but at the live edge it requests the next one before it is
published, fails 100 times without waiting and gives up ("Reached max
consecutive fragment open failures"), with both `SegmentTimeline` and
`$Number$` manifests. The ingestor then reconnects after `--reconnect_delay`,
so you get a burst of segments every few seconds - samples of the stream,
not continuous coverage. If the packager also offers HLS for the same stream
(most do, often with the same CMAF segments), use the HLS URL instead.

To pick one rendition of a multi-bitrate HLS/DASH stream, use `--map`, e.g.
`--map 0:p:2:v:0` for the video of the third program; `ffprobe <url>` lists
the programs.

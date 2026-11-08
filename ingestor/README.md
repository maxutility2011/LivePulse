# Ingestor

The ingestor turns **one live stream** into an **HLS stream with fragmented
MP4 segments** - a media playlist, an init segment and a sequence of media
segments - and publishes it to a cloud bucket or to the Nginx webroot inside
its container. It runs as a Docker service with FFmpeg and Nginx.

| input | `--port` is | how it is segmented |
|---|---|---|
| RTMP push | local port to listen on | stream copy, cut on GOP boundaries |
| SRT push (listener) | local port to listen on | stream copy, cut on GOP boundaries |
| MPEG-TS over UDP / RTP, unicast or multicast | local port to receive on | stream copy, cut on GOP boundaries |
| SRT pull (caller) | remote port to pull from | stream copy, cut on GOP boundaries |
| HLS (TS, CMAF or fMP4 segments) | remote port to pull from | passed through segment by segment |
| DASH (CMAF / fMP4) | remote port to pull from | passed through segment by segment, republished as HLS |

The video is never re-encoded.

## Running with Docker

```bash
docker build -t livepulse-ingestor ingestor/

# SRT push -> served by the container's Nginx
docker run -d --name ingest-cam1 -p 9000:9000/udp -p 8080:80 livepulse-ingestor \
    --input_url "srt://0.0.0.0?latency=500000" --port 9000 --stream_id cam1
# play http://<host>:8080/cam1/index.m3u8

# RTMP push to rtmp://<host>:1935/live/cam2 -> GCS
docker run -d -p 1935:1935 -e GOOGLE_APPLICATION_CREDENTIALS=/secrets/gcs.json \
    -v $PWD/gcs.json:/secrets/gcs.json:ro livepulse-ingestor \
    --input_url rtmp://0.0.0.0/live/cam2 --port 1935 --stream_id cam2 --cloud_upload_url gs://my-bucket/live

# HLS pull -> S3
docker run -d -e AWS_ACCESS_KEY_ID=... -e AWS_SECRET_ACCESS_KEY=... -e AWS_REGION=us-east-1 livepulse-ingestor \
    --input_url https://cdn.example.com/live/master.m3u8 --port 443 --stream_id cdn1 --cloud_upload_url s3://my-bucket/live
```

[`docker-compose.yml`](docker-compose.yml) has the same as Compose services.
Run one container per stream; the containers are independent, so they scale
out across hosts (or as Kubernetes Deployments) with no shared state.

**Ports.**

- Publish the ingest port with `-p`. SRT, UDP and RTP are UDP: use
  `-p 9000:9000/udp`. RTMP is TCP (`-p 1935:1935`). Pull inputs (SRT caller,
  HLS, DASH) connect out and need no published port.
- Nginx listens on port 80 in the container (`-p 8080:80`). Set
  `LP_DISABLE_NGINX=1` to skip it when uploading to a bucket.
- Uploads to GCS / S3 are outgoing HTTPS connections to port 443; outgoing
  traffic needs no `-p` or `EXPOSE`.

**Multicast** only reaches a container that shares the host's network:
`--network host` on a Linux host (Docker Desktop on Windows/macOS can't
receive multicast).

**SRT over lossy links.** SRT can resend lost packets only within its
`latency` window (120 ms by default). Raise it on both ends for lossy or
long-distance links, e.g. `srt://0.0.0.0?latency=500000` (microseconds).
Through Docker Desktop's port forwarding on Windows, the default lost enough
UDP packets at 4 Mbit/s to corrupt frames; 500 ms - 1 s fixed it. Raw UDP /
RTP MPEG-TS has no retransmission at all, so prefer host networking for it.

## Output

```
<webroot or bucket prefix>/<stream_id>/
    index.m3u8          media playlist (sliding window of --hls_list_size segments)
    init_0001.mp4       init segment (a new one after each reconnect)
    seg_00000001.m4s    media segments
    seg_00000002.m4s
    ...
```

- Without `--cloud_upload_url` the files go to `<--nginx_webroot>/<stream_id>/`
  (default `/var/www/html/cam1/...`) and Nginx serves them with HLS content
  types, CORS and `Cache-Control: no-cache` for the playlist.
- With `--cloud_upload_url gs://bucket/prefix` or `s3://bucket/prefix` they go
  to `<prefix>/<stream_id>/` in the bucket, with `Content-Type` and
  `Cache-Control` set (playlist `no-cache, max-age=1`, segments long-lived).
  Uploads run on a background thread, in order, with retries, so a slow
  bucket never makes the ingestor miss input; each new playlist is uploaded
  after the segments it lists.
- Segments that slide out of the playlist are deleted three segments later
  (so clients holding an older playlist can still fetch them); init
  segments are deleted once no segment needs them. `--hls_list_size 0`
  keeps everything (EVENT playlist).
- After a reconnect the next segment is marked `#EXT-X-DISCONTINUITY` and
  gets the new run's init segment. When the ingestor stops (Ctrl+C or
  `docker stop`), the open segment is finished and the playlist ends with
  `#EXT-X-ENDLIST` (unless `--no_endlist`).

**Credentials.** GCS uses Application Default Credentials: mount a
service-account key and set `GOOGLE_APPLICATION_CREDENTIALS`, or use workload
identity on GKE. S3 uses the standard AWS settings (`AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, `AWS_REGION`, or an instance / IRSA role);
`AWS_ENDPOINT_URL` points it at an S3-compatible store. Making the objects
readable by players (public bucket, signed URLs or a CDN in front) is up to
the bucket configuration.

## Segmenting continuous inputs on GOP boundaries

For RTMP, SRT and MPEG-TS the stream is cut only at keyframes, without
re-encoding: each segment holds the **fewest whole GOPs whose total length
reaches `--segment_seconds`**. With `--segment_seconds 5` and a 2 s GOP every
segment has 3 GOPs = 6 s; with a 2.5 s GOP, 2 GOPs = 5 s. (FFmpeg's own HLS
muxer cuts against cumulative targets and would alternate 6 s and 4 s
segments here.)

How: FFmpeg stream-copies the input into fMP4 HLS with a tiny `hls_time`, so
it cuts at every keyframe - one fragment (`moof`+`mdat`) per GOP, all sharing
one init segment, with exact durations in its playlist under
`--staging_dir`. The ingestor concatenates the fragments of consecutive GOPs
into one media segment; several fragments per segment are valid fMP4/CMAF.
Segment lengths follow the GOP structure, so irregular GOPs (scene-cut
keyframes) give irregular segments; `#EXT-X-TARGETDURATION` grows to the
longest one.

Audio: the first audio stream is copied (`--audio copy`), which expects AAC
(ADTS AAC from MPEG-TS is converted to the MP4 form). For other audio codecs
use `--audio aac` (re-encode audio only) or `--audio none`.

## HLS and DASH inputs

**HLS:** the media playlist - or the single variant of a master playlist
(the highest bandwidth one if there are several) - is polled, and each new
segment is copied byte for byte, with its `EXT-X-MAP` init segment,
`EXTINF` duration and discontinuities. TS segments stay TS. Byte-range
segments are supported, encrypted ones (`EXT-X-KEY`) are not.

**DASH:** the MPD is polled and the single video Representation (the highest
bandwidth one if there are several) is followed: `SegmentTemplate` with
`SegmentTimeline` or with `@duration` (`$Number$` / `$Time$`), multi-period
streams (a new period starts a discontinuity). Init and media segments are
copied byte for byte and listed in an HLS playlist with `EXT-X-MAP`. This
client waits for segments that are announced but not yet published, so live
DASH works (FFmpeg's DASH demuxer gives up at the live edge). Not supported:
`SegmentList` / `SegmentBase`, DRM, and audio / text AdaptationSets
(video only).

Both start three segments behind the live edge.

## Options

| option | default | |
|---|---|---|
| `--input_url URL` | (required) | `rtmp://`, `srt://`, `udp://`, `rtp://` or `http(s)://`. When listening, the host is the bind address (or multicast group for UDP/RTP) and the RTMP path is the app/stream key (default `/live/stream`); when pulling, it is the remote host. Query options pass through to FFmpeg, e.g. `?latency=500000&passphrase=...` for SRT, `?localaddr=10.0.0.5` for multicast |
| `--port N` | (required) | local port to listen on (RTMP/SRT push, UDP/RTP) or remote port to pull from (SRT pull, HLS, DASH). If the URL has a port too, they must match |
| `--input_mode listen\|pull` | listen for rtmp/srt/udp/rtp, pull for http(s) | `pull` with `srt://` = SRT caller |
| `--input_format auto\|hls\|dash` | `auto` | for http(s): by `.m3u8` / `.mpd` extension, else by content |
| `--segment_seconds S` | `6` | minimum segment length; continuous inputs get the fewest whole GOPs reaching it |
| `--audio copy\|aac\|none` | `copy` | continuous inputs: copy the first audio stream, re-encode it to AAC, or drop it |
| `--audio_bitrate B` | `128k` | for `--audio aac` |
| `--stream_id NAME` | `live` | output folder / object prefix |
| `--cloud_upload_url URL` | none | `gs://bucket[/prefix]` or `s3://bucket[/prefix]`; without it, files go to the webroot |
| `--nginx_webroot DIR` | `/var/www/html` | webroot (Nginx is pointed at it automatically) |
| `--playlist_name NAME` | `index.m3u8` | media playlist name |
| `--hls_list_size N` | `10` | segments in the live playlist; `0` keeps all (EVENT playlist) |
| `--staging_dir DIR` | `/tmp/lp_storage` | scratch space for FFmpeg's per-GOP fragments |
| `--stall_timeout S` | `15` | reopen an SRT/UDP/RTP input after this many seconds without data (`0` = never) |
| `--reconnect_delay S` | `3` | wait before reopening a dropped continuous input; negative = exit instead |
| `--input_args "..."` | none | extra FFmpeg input options; write it as `--input_args="-re"` |
| `--max_segments N` | `0` | stop after publishing N segments (testing) |
| `--no_endlist` | off | don't end the playlist when stopping |
| `--ffmpeg_path`, `--log_level` | | |

Container environment: `LP_DISABLE_NGINX=1` skips Nginx; cloud credentials
as above.

## Without Docker

`python ingestor/lp_ingestor.py ...` with the same options works anywhere
with Python 3.10+ and FFmpeg (`pip install -r ingestor/requirements.txt` for
cloud uploads). Pass `--nginx_webroot` and `--staging_dir` paths that exist
on your machine, e.g.
`--nginx_webroot ./www --staging_dir ./stage`.

## Tested

With FFmpeg 7.1 in the container (and 9.0 on Windows), a fixed-GOP H.264 +
AAC test stream, and `--segment_seconds 5` / 2 s GOP:

- SRT push, RTMP push, UDP unicast and multicast, SRT pull: 6.000 s segments
  of 3 GOPs, video + audio, playable over HTTP from the container's Nginx.
- HLS pull with TS and with fMP4 segments, DASH pull with `SegmentTimeline`
  and with `$Number$` + `@duration`: segments passed through with their
  durations, following the live edge.
- Uploads to S3 (S3Mock) and GCS (fake-gcs-server): objects, content types,
  cache headers, sliding-window playlist, playable after download.
- `docker stop`: the open segment is finished and the playlist ended in
  about a second.

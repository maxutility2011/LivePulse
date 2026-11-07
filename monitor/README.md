# Monitor

`lp_monitor.py` follows one or more `scores.jsonl` files written by the
[processor](../processor/README.md) and tracks every stream:

- the last UVQ score and the rolling mean / min / max over `--window`
- segment counts by status (`ok`, `error`, `skipped`)
- a **low-quality alert** when the score stays below `--low_score` for
  `--alert_after` segments in a row, and a **stale alert** when no new
  segment arrives for `--stale_after` (ingest or processing stopped)

It logs alerts as they change (`ALERT` / `RECOVERED`, `STALE` / `RESUMED`),
prints a report every `--report_interval`, and can export everything to
Prometheus/Grafana, Datadog and a JSON file. It uses only the Python
standard library.

```bash
python3 monitor/lp_monitor.py live/cam1/scores.jsonl
python3 monitor/lp_monitor.py "live/*/scores.jsonl" --prometheus_port 9108 --statsd 127.0.0.1:8125 --tags env:prod
```

```
[2026-11-07 12:53:40] ALERT cam1: UVQ below 4.62 for 1 segments (last 4.619, seg_20261107-125327-057_00002.mp4)
[2026-11-07 12:53:43] cam1: last 4.619  5m mean 4.621 min 4.619 max 4.623 (3 seg)  [ok 3] [LOW QUALITY]
[2026-11-07 12:53:45] RECOVERED cam1: UVQ 4.632 after 1 low segments
[2026-11-07 12:54:04] STALE cam1: no new segment for 15s
```

On start it reads the existing history of each file (without logging old
alerts), then follows new lines. Wildcards are re-checked, so streams added
later are picked up.

## Options

| option | default | |
|---|---|---|
| `scores ...` | (required) | one or more scores files; wildcards are expanded |
| `--window T` | `5m` | rolling window for mean/min/max (`90s`, `5m`, `1h`, ...) |
| `--low_score X` | `3.0` | UVQ score counted as low quality (UVQ ranges 1-5) |
| `--alert_after N` | `3` | low segments in a row before the low-quality alert fires |
| `--stale_after T` | `60s` | stale alert when a stream has no new segment for this long; keep it above the segment length plus scoring time |
| `--report_interval T` | `60s` | console report period |
| `--summary_json FILE` | none | rewrite a JSON summary of all streams every 10 s |
| `--prometheus_port N` | off | serve Prometheus metrics at `http://<host>:N/metrics` |
| `--prometheus_host ADDR` | `0.0.0.0` | bind address for the metrics endpoint |
| `--statsd HOST:PORT` | off | send DogStatsD metrics (UDP), e.g. to a Datadog agent on `127.0.0.1:8125` |
| `--statsd_prefix P` | `livepulse` | DogStatsD metric prefix |
| `--tags a:b,c:d` | none | extra DogStatsD tags on every metric |

## Grafana (via Prometheus)

Add the monitor as a Prometheus scrape target:

```yaml
scrape_configs:
  - job_name: livepulse
    scrape_interval: 15s
    static_configs:
      - targets: ["monitor-host:9108"]
```

| metric | type | labels | |
|---|---|---|---|
| `livepulse_uvq_score` | gauge | `stream` | score of the newest segment |
| `livepulse_uvq_score_window_mean` / `_min` / `_max` | gauge | `stream`, `window` | rolling statistics |
| `livepulse_segments_total` | counter | `stream`, `status` | segments seen by status |
| `livepulse_seconds_since_last_segment` | gauge | `stream` | age of the newest segment |
| `livepulse_low_quality_alert` | gauge | `stream` | 1 while the low-quality alert is active |
| `livepulse_stale_alert` | gauge | `stream` | 1 while the stream is stale |

In Grafana, plot e.g. `livepulse_uvq_score_window_mean` per stream, and alert
on `livepulse_low_quality_alert == 1` or `livepulse_stale_alert == 1`.

## Datadog (via DogStatsD)

Point `--statsd` at a Datadog agent with DogStatsD enabled (port 8125 by
default). Every metric carries `stream:<id>` plus `--tags`:

| metric | type | sent | |
|---|---|---|---|
| `livepulse.uvq.score` | gauge | per segment | segment score |
| `livepulse.segments` | count | per segment | tagged `status:ok/error/skipped` |
| `livepulse.uvq.window.mean` / `.min` / `.max` | gauge | every 10 s | rolling statistics |
| `livepulse.alert.low_quality`, `livepulse.alert.stale` | gauge | every 10 s | 0 or 1 |

Build Datadog monitors on `livepulse.uvq.window.mean` or the alert gauges.

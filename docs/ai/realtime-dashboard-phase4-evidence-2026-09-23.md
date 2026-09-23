# Realtime dashboard — Phase 4 evidence

Date: 2026-09-23
Command:

```bash
PYTHONPATH=. /home/vc/ceph-ai/.venv/bin/python \
  scripts/realtime_dashboard_benchmark.py --reads-per-tab 100
```

## Server-side baseline

The benchmark uses the persisted snapshot read model. It does not call Ceph or
SSH, so the query-rate result is directly attributable to the dashboard read
path.

| Simulated tabs | Reads | p50 | p95 | p99 | Ceph/SSH queries |
|---:|---:|---:|---:|---:|---:|
| 1 | 100 | 0.075 ms | 0.119 ms | 0.127 ms | 0 |
| 5 | 500 | 0.482 ms | 0.705 ms | 0.980 ms | 0 |
| 10 | 1,000 | 0.968 ms | 1.701 ms | 2.314 ms | 0 |

Target: p95 read latency <= 200 ms. All three server-side baselines pass.
This is a cache/read-path result, not a full browser-render or network result.

## Integrity and isolation checks

- `payload_checksum` is SHA-256 over canonical snapshot payload data.
- `checksum_valid=true` for both cluster A and cluster B.
- Metadata-only changes (`refreshing`, stale/error metadata) preserve the
  checksum; a payload change produces a different checksum.
- Same-cluster refresh claims are single-flight: 1 successful claim from 8
  concurrent attempts.
- Cluster B can claim its own refresh independently.
- Cluster A and B return different `cluster_id`, payload and checksum values.
- Snapshot reads perform 0 additional Ceph/SSH queries.

## State and telemetry contract

- Health API now returns `state=ready|stale|partial|error`, checksum metadata,
  and the existing freshness/error fields.
- Collector metrics expose `last_collector_lag_ms`, `ssh_calls_total`,
  `queue_depth` and `max_queue_depth`.
- API observability exposes p50/p95/p99 latency overall and per route.
- Health/Pools keep an independent HTTP polling loop. WebSocket messages are
  invalidation hints only; reconnect uses bounded exponential backoff. This
  means a failed WebSocket does not stop snapshot reads.

## Chrome browser benchmark

Run after `npm ci` and `npm run build` in `ceph-health-dashboard`:

```bash
RT_BENCH_PYTHON=/path/to/python-with-project-dependencies \
  npm run benchmark:realtime
```

The Playwright runner starts an isolated FastAPI instance with temporary SQLite
and snapshot cache, signs in through the normal login route, and opens 1, 5,
and 10 real Chrome tabs. Each group also makes 20 browser-side API probes.
The temporary fixture is removed after the run.

| Tabs | HTML TTFB p95 | Browser API p95 (20 probes) | Passive API p95 | HTTP poll rate | Ceph/SSH commands added |
|---:|---:|---:|---:|---:|---:|
| 1 | 56.3 ms | 4.7 ms | 169.0 ms (2 samples) | 0.2/s | 0 |
| 5 | 14.3 ms | 11.1 ms | 123.1 ms (10 samples) | 1/s | 0 |
| 10 | 19.6 ms | 23.5 ms | 28.4 ms (20 samples) | 2/s | 0 |

The local HTML TTFB and browser API p95 values meet the 200 ms target. The
`passive API p95` for one tab has only two observations and is not a stable
quantile. The `all pages ready` wall time was 455 ms, 552 ms and 795 ms for
1/5/10 tabs, respectively; that includes browser rendering and is not the
HTML/API TTFB target.

Each tab opened one scoped `/ws/cluster-state` connection and one legacy
`/ws/incidents` connection. The browser received the B-only invalidation
event in cluster B; cluster A received no B event and kept its own health.
With the cluster WebSocket blocked, HTTP polling updated the health card in
about 1.97 seconds. The server-side API registry recorded p95 5.95 ms and
remote command executions remained at zero.

The browser run exposed and verified fixes for two defects:

1. Overview `/` lacked `data-cluster-id`, so its cluster event hook did not
   subscribe when the URL had no explicit cluster query parameter.
2. An older `status` section could override a newer critical health value,
   preventing the UI from showing a new `HEALTH_ERR`.

This benchmark evidence is from a local isolated fixture. The critical health
precedence fix was applied as a focused patch to `10.3.55.213:/root/ceph-ai`;
the server's existing dirty worktree was preserved. A regression test was
added there; its focused health API suite passed (8 tests), and the Dashboard
container was restarted and reached `healthy`. A live
1/5/10-tab benchmark, real Ceph health-change SLA, and longer network/reconnect
soak still require separate measurements.

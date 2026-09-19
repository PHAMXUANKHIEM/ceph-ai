# Realtime snapshot read-load evidence

Target: `10.3.55.213:/root/ceph-ai`

Command:

```text
.venv/bin/python - <<'PY'
from concurrent.futures import ThreadPoolExecutor
from time import perf_counter
from shared.cluster_snapshot import read_snapshot, snapshot_fingerprint
...
PY
```

The harness performed 100 persisted snapshot reads per simulated tab. It only
called the snapshot read path; it did not run Ceph, SSH, or a Watcher refresh.

| simulated tabs | reads | elapsed | p95 read latency | Ceph/SSH queries |
|---:|---:|---:|---:|---:|
| 1 | 100 | 0.020 s | 0.500 ms | 0 |
| 5 | 500 | 0.214 s | 1.838 ms | 0 |
| 10 | 1000 | 0.383 s | 1.978 ms | 0 |

Snapshot fingerprint remained stable during all reads:
`(1789824055233000000, 6394)`.

This proves the server-side snapshot path is bounded and does not open live
Ceph/SSH work per read. A real browser 1/5/10-tab run with DevTools/network
and canary authentication is still required before the browser-load checklist
is closed.

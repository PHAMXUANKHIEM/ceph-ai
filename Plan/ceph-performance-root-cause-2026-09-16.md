# Ceph-AI performance baseline and root-cause report

Date: 2026-09-16
Target: `10.3.55.213:/root/ceph-ai`

## Scope and method

The repository was inspected across `dashboard/`, `watcher/`, `worker/`,
`config/` and `shared/`, including Paramiko, subprocess, Ceph command, route,
cache, snapshot and frontend refresh call sites. The default active cluster
was queried read-only once for health and once for full status. No mutating
Ceph command was run.

The Croit reference was accessible as a public MCP Ceph management repository.
The relevant principles are API-based access, field selection, filtering,
smart summaries, caching, automatic limits, drill-down for large results,
RBAC and explicit error handling. The repository README also documents a
local OpenAPI specification to avoid repeated startup fetches and a faster
optimized path. These principles support the snapshot and bounded-payload
direction in `Plan/ceph-performance-optimization-plan.md`; no Croit code was
copied.

## Baseline measurement

Environment: active default cluster `CS-LAB`, three configured MON nodes,
read-only calls from the dashboard container, one sample per operation.

| Operation | Result | Elapsed |
| --- | --- | ---: |
| `query_cluster_health_with` | `HEALTH_WARN` | 3465.6 ms |
| `query_cluster_status_with` (`ceph -s` JSON) | payload returned | 6563.3 ms |
| `/api/dashboard/health` | not measured with an authenticated browser session in this phase | — |
| Dashboard initial browser load | not measured in this phase | — |

The two measurements are a directional baseline, not a p95 benchmark. A
repeatable multi-sample benchmark belongs to Phase 11.

## Diagnosis table

| Component | Problem | Evidence in code | Impact | Proposed solution |
| --- | --- | --- | --- | --- |
| `watcher/ceph_client.py::_run_remote_command_with` | A new Paramiko client is created for each remote command. | Lines 1017–1029 construct `SSHClient` and call `connect`; lines 1058–1063 close it. | Connection setup is repeated for every command and every fallback node. | Add a bounded per-cycle connection pool/runner with health checks and guaranteed cleanup. |
| `watcher/ceph_client.py::query_cluster_health_with` | MON fallback is sequential. | Lines 1354–1364 loop over MONs and wait for each failure before trying the next. | A slow first MON adds its full timeout before a healthy MON is used. | Use bounded per-node tasks where safe, a total deadline and `return_exceptions=True`; preserve a successful-node fallback. |
| `watcher/ceph_client.py::_run_remote_command_with` | Timeout coverage is incomplete for total runtime and output size. | Lines 1046–1053 use Paramiko channel timeout and unbounded `.read()` calls; lines 1005 and 1037 use module constants rather than central settings. | Silent commands, large output and channel behavior can delay completion or consume excessive memory. | Add separate connect/banner/auth/command deadlines, bounded reads, process termination and structured timeout errors. |
| `worker/executor/ssh_executor.py::execute_command_bytes` | Remediation SSH uses a long fixed command timeout. | Lines 11–16 define connect=5 and command=1800; lines 47–58 execute and read output. | A stuck command may occupy a worker for up to 30 minutes. | Keep long limits only for explicitly long operations; use central per-action budgets and hard total deadlines. |
| `worker/backup/engine.py` | Backup export opens its own SSH client and has no explicit `exec_command` timeout. | Lines 458–486 connect and read the export stream; the command does not pass a timeout. | A blocked export can hold a worker and temporary file indefinitely. | Route through a bounded streaming runner with cancellation, byte limits and an action-specific deadline. |
| `dashboard/routes/block_storage.py::_query_block_storage` | The page still performs inventory collection on a cache miss. | Lines 185–193 call `get_or_load` in a thread, but the loader executes pool, namespace and image commands. | First page load can wait for a large inventory; a thread prevents event-loop blocking but not slow user response. | Move inventory collection to a scheduled/heavy job and serve an explicit snapshot/loading state. |
| `dashboard/routes/volumes.py` cache loaders | Several volume operations use cache wrappers but still perform live Ceph work on a miss. | Lines 108–147 define `_cached_rbd_trash`, `_cached_rbd_iostat` and `_cached_rbd_inventory`; lines 560 and 608 invoke them through route paths. | A cold or expired cache can still produce a slow request. | Give each heavy section a persisted job/snapshot and a bounded stale-if-error response. |
| `shared/object_storage_cache.py::get_or_load` | Cold cache loads synchronously in the calling thread. | Lines 67–80 call `loader()` unless `background_on_miss` is true. | Any caller that does not wrap it in `to_thread` can block its request worker; the cache also has only a process-local entry map. | Make the background miss policy explicit for heavy data and use a cross-process versioned snapshot store where required. |
| `watcher/cluster_snapshot_collector.py` | Snapshot architecture already exists but health and status are collected as separate sections and calls. | Lines 119–164 state separate health/status collection; lines 169–190 publish status independently. | Fast health can be available before status, but overall dashboard freshness can still reflect the slower section. | Keep independent sections, publish timing/error metadata, and enforce one shared cycle deadline. |
| `shared/cluster_snapshot.py` | Versioning and refreshing state exist, but the schema does not yet provide the full requested collector timing envelope. | Lines 97–130 publish versioned snapshots/events; lines 133–165 persist refreshing state; lines 223–245 preserve section data on error. | Good stale behavior exists, but node-level timings and standardized error kinds are not universal. | Extend the envelope with duration, attempt timestamps, partial/error kinds and node/command latency. |
| `dashboard/routes/incidents.py::dashboard_health` | The main health API is already snapshot-backed. | Lines 1018–1030 read `read_snapshot`; lines 1033–1045 only enqueue refresh. | This path is structurally correct and should not be replaced with live SSH. | Preserve the contract and verify p95/cache-hit behavior with tests and middleware metrics. |
| `watcher/main.py` auxiliary scans | Heavy scans are isolated by separate cadences and background helpers, but many operations remain in one large watcher process. | Lines 1207–1244 run node/BlueStore scans; lines 1269–1334 run CRUSH, topology, host metrics and RCA auxiliary work. | Resource contention can still affect the poll process even with independent `try/except` blocks. | Add explicit poll deadlines, bounded auxiliary concurrency and metrics; keep AI diagnosis outside critical health collection. |
| `dashboard/routes/nodes.py`, `dashboard/routes/pgs.py` | These routes already read section snapshots. | `nodes.py` lines 32–100 and `pgs.py` lines 294–410 use `read_section_snapshot`; comments explicitly state no SSH from the route. | This is an existing successful pattern. | Use it as the migration template for remaining heavy pages. |
| `dashboard/routes/object_storage.py` and `object_storage_users.py` | Some inventory endpoints are cached and moved to threads, but payload size and cold-load cost need measurement. | `object_storage.py` lines 983–1211 and `object_storage_users.py` lines 231–255 define cached inventory paths. | Large lists can still increase latency and response size. | Add limits, page metadata, timing/size metrics and background refresh where missing. |
| correlation and metrics | No single end-to-end latency contract is visible across API, queue, snapshot, SSH and command stages. | Existing logs/metrics are distributed across modules; the inspected paths do not share a universal request correlation ID. | Root cause analysis of production slowness is difficult. | Add middleware correlation IDs and stage metrics in Phase 9. |

## Existing strengths to preserve

- `/api/dashboard/health` reads the shared snapshot and its refresh endpoint
  queues work without waiting for Ceph.
- `shared/cluster_snapshot.py` provides versioned snapshots, events, refresh
  markers, stale handling and last-good section preservation.
- Nodes and Pools/PG APIs already follow section snapshot reads.
- Block Storage and several volume pages use `asyncio.to_thread` and stale
  cache behavior, which prevents some event-loop stalls.
- Paramiko host-key pinning and `finally` cleanup already exist in the primary
  watcher and executor paths.
- Existing action policy, approval and audit paths must remain untouched while
  moving data collection.

## Prioritized implementation order

1. Centralize timeout/retry/concurrency settings and add tests.
2. Introduce the shared runner for watcher and read-only dashboard collectors.
3. Add bounded batched collection and standardized partial errors.
4. Complete per-cluster snapshot metadata and single-flight refresh behavior.
5. Migrate remaining fast APIs and heavy APIs separately.
6. Add watcher scheduling deadlines and resource metrics.
7. Add frontend stale/loading behavior only where backend metadata is present.
8. Add payload limits, observability, failure tests and benchmark evidence.

## Phase 0 status

- [x] Source inventory across the requested subsystems.
- [x] Direct SSH/subprocess/Ceph call-site search.
- [x] Snapshot/cache architecture review.
- [x] Read-only health/status baseline.
- [x] Reference-principle review of accessible Croit repository material.
- [x] Root-cause table with file/function evidence.
- [ ] Authenticated browser timing and p95 benchmark; scheduled for Phase 11.

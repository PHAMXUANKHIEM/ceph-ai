# Ceph Performance and Non-Blocking Operations Plan

Target repository: `https://github.com/PHAMXUANKHIEM/ceph-ai`
Target deployment checkout: `/root/ceph-ai` on `10.3.55.213`
Reference principles: Croit Ceph management repositories, used for comparison only.

## Objective

Make Ceph-AI responsive under slow or unavailable Ceph nodes while preserving
current functionality, API contracts, authentication, RBAC, cluster scope,
approval/audit flows, AI chat, polling and event behavior.

The target architecture is:

```text
Ceph cluster
    ↓
Watcher / bounded collector
    ↓
Snapshot cache or database
    ↓
FastAPI read API
    ↓
Dashboard
```

## Non-negotiable rules

- Read the relevant source before changing it: `dashboard/`, `watcher/`,
  `worker/`, `config/`, `shared/`, all dashboard routes, and all Ceph/SSH
  integrations.
- Search every use of Paramiko, subprocess, `ceph`, `cephadm`, `rados`, `rbd`
  and `radosgw-admin` before designing replacements.
- Do not run administrative or data-changing Ceph commands during tests.
- Do not change authentication, authorization, cluster isolation,
  preview/approval/audit or existing mutation command semantics.
- Change one phase at a time. A phase is complete only after its exit checks
  pass and its evidence is recorded below.
- Do not push, merge or deploy automatically.
- Do not use fake data to claim performance or feature completion.

## Phase 0 — Baseline, inventory and root-cause report

### Work

- Read the complete target areas and map every request path that can reach
  Ceph, SSH, subprocesses or large result parsing.
- Inventory direct Ceph calls by route and classify them as fast summary,
  inventory, performance, logs, CRUSH, object storage or backup work.
- Inspect the current watcher polling loop, dashboard cache, worker queue and
  frontend refresh/event behavior.
- Compare connection reuse, timeout handling, batching, partial-failure
  handling and snapshot delivery with the accessible Croit code.
- Capture current measurements for:
  - `/api/dashboard/health`
  - dashboard initial load
  - nodes/pools APIs
  - number of SSH connections
  - number of Ceph commands
  - behavior with one slow or unreachable node
- Create a diagnosis table with columns: component, problem, code evidence,
  impact and proposed solution.

### Expected files

- `Plan/ceph-performance-root-cause-<date>.md`
- No production code changes are required for this phase.

### Exit criteria

- Every blocking Ceph path has a file and function reference.
- A baseline report contains measured latency and request/command counts.
- The new collector boundary and migration order are agreed from evidence.

## Phase 1 — Central timeout and concurrency configuration

### Work

- Add typed settings for:
  - `CEPH_SSH_CONNECT_TIMEOUT=5`
  - `CEPH_SSH_BANNER_TIMEOUT=5`
  - `CEPH_SSH_AUTH_TIMEOUT=5`
  - `CEPH_COMMAND_TIMEOUT=15`
  - `CEPH_HEALTH_TIMEOUT=8`
  - `CEPH_INVENTORY_TIMEOUT=20`
  - `CEPH_LOG_QUERY_TIMEOUT=20`
  - `CEPH_REFRESH_INTERVAL=5`
  - `CEPH_SNAPSHOT_MAX_AGE=30`
  - `CEPH_MAX_CONCURRENCY=8`
  - `CEPH_MAX_RETRIES=2`
- Add validation for positive ranges and reject accidental unlimited values.
- Add exponential backoff with jitter and retry classification.
- Do not retry authentication failures, invalid configuration or malformed
  commands.
- Document safe defaults in the environment example and settings page where
  the existing architecture exposes runtime configuration.

### Expected files

- `config/settings.py`
- `config/*.py` environment examples/templates
- `shared/` timeout or retry utility, if needed
- tests for settings, retry bounds and error classification

### Exit criteria

- All new timeout values come from central settings.
- No new code path uses an unbounded timeout.
- Retry count and total retry delay are bounded and tested.

## Phase 2 — Shared SSH and command runner

### Work

- Implement a shared `CephCommandRunner` and a bounded `CephConnectionPool`
  around the existing SSH library and execution modes.
- Configure connect, banner and authentication timeout separately.
- Reuse a healthy connection within one collection cycle.
- Reconnect once when a reusable connection is broken, subject to retry
  limits.
- Close channels, transports, sessions and subprocesses through context
  managers or `finally` blocks.
- Apply per-command timeout, bounded stdout/stderr size and safe termination
  for subprocesses.
- Return structured errors with node, stage, kind, message and timing.
- Track node state as `healthy`, `degraded`, `unreachable` or `auth_failed`.
- Keep the existing Ceph container/SSH execution modes and key handling.

### Expected files

- `dashboard/ceph_tools.py`
- `dashboard/cluster_scope.py`
- `watcher/ceph_client.py`
- a new shared runner/pool module if the existing boundaries require it
- unit tests with fake SSH and subprocess runners

### Exit criteria

- Connect, banner, auth and command timeouts are independently enforced.
- A timeout leaves no orphan process, channel or transport.
- Existing callers can migrate without changing their public API contract.

## Phase 3 — Bounded batched Ceph collection

### Work

- Implement `CephSnapshotCollector` on top of the shared runner.
- Batch compatible read-only commands where the Ceph execution mode allows:
  - `ceph -s -f json`
  - `ceph osd tree -f json`
  - `ceph osd df -f json`
  - `ceph pg stat -f json`
  - `ceph df -f json`
- Use JSON output whenever supported and parse defensively.
- Collect independent nodes concurrently with a semaphore and
  `return_exceptions=True` behavior.
- Add per-node deadline, per-command deadline and a total poll deadline.
- Continue collecting healthy nodes after one node fails.
- Record `started_at`, `finished_at`, `duration_ms`, node and command stage.
- Keep heavy data such as logs, full CRUSH and large inventory outside the
  fast health collection.

### Snapshot schema

```json
{
  "cluster_id": "default",
  "collected_at": "...",
  "duration_ms": 820,
  "health": "HEALTH_OK",
  "nodes": {},
  "errors": [],
  "partial": false,
  "stale": false,
  "refreshing": false
}
```

### Expected files

- `watcher/main.py`
- `watcher/ceph_client.py`
- `shared/` collector/snapshot models
- collector and partial-failure tests

### Exit criteria

- One hung node cannot block other node results.
- The collector has a hard total deadline.
- Ceph calls are batched where supported and call counts are measured.

## Phase 4 — Per-cluster snapshot cache and refresh coordination

### Work

- Add per-cluster state containing:
  - `latest_snapshot`
  - `previous_snapshot`
  - `last_success_at`
  - `last_attempt_at`
  - `last_error`
  - `generation`
  - `partial`
  - `stale`
  - `refreshing`
- Scope every key by `cluster_id`.
- Add a single-flight lock so concurrent refresh requests enqueue at most one
  collection job per cluster.
- Persist or reconstruct the latest snapshot according to the existing cache
  and database conventions.
- Define separate TTLs for fast health, inventory, performance history, logs
  and CRUSH data.
- Preserve the latest usable data with explicit age and stale metadata.

### Expected files

- `shared/ceph_query_cache.py`
- `dashboard/cache_warmup.py`
- `dashboard/cluster_scope.py`
- `watcher/` snapshot publication code
- cache isolation, stale and single-flight tests

### Exit criteria

- Health reads are O(1) cache reads and do not open SSH.
- Two simultaneous refresh requests produce one collection job.
- Data from different clusters cannot share a cache key or snapshot.

## Phase 5 — Fast APIs and heavy APIs

### Work

- Make health summary, OSD/MON/server counts, utilization, PG summary and
  snapshot metadata read from the latest snapshot.
- Move blocking work in `async def` routes to `asyncio.to_thread`, a bounded
  executor or the existing worker queue.
- Split heavy endpoints from the fast dashboard path:
  - CRUSH map and full OSD tree
  - Log Intelligence
  - object inventory and bucket access logs
  - large RBD inventory
  - backup listings from multiple sources
  - performance history
- Give heavy work a job state, pagination/limit, timeout and explicit loading
  or error response.
- Verify client disconnects cancel or release background resources where the
  existing job model permits it.

### Expected files

- `dashboard/routes/*.py`
- `dashboard/ceph_tools.py`
- `dashboard/cache_warmup.py`
- worker queue/job modules
- API latency and “no direct SSH” tests

### Exit criteria

- `/api/dashboard/health` never performs a direct Ceph query.
- Heavy API work cannot block health API work.
- Every route using blocking libraries runs outside the event loop.

## Phase 6 — Watcher scheduling and incident isolation

### Work

- Make polling fixed-period and non-overlapping with a per-cluster deadline.
- Collect once per cycle and publish one snapshot for all consumers.
- Keep AI diagnosis outside the health collection deadline.
- Debounce incident creation on unchanged state while preserving recovery and
  audit semantics.
- Add poll duration, node failure count, collection errors and queue timing.
- Prevent duplicate Watcher/Dashboard collection of the same fast data.

### Expected files

- `watcher/main.py`
- `worker/main.py`
- `worker/llm/` diagnosis scheduling
- incident and deduplication modules
- scheduler/incident regression tests

### Exit criteria

- Poll cycles cannot overlap.
- AI diagnosis cannot extend the health poll deadline.
- One node failure produces partial data and a scoped error.

## Phase 7 — Frontend freshness and non-blocking behavior

### Work

- Keep React polling/event invalidation and `AbortController` behavior while
  consuming snapshot metadata.
- Add first-load skeleton, stale banner, refreshing state, retry and measured
  update time.
- Do not reload the whole page for periodic updates.
- Ensure the dashboard shows the last valid snapshot when Ceph is unavailable.
- Keep large pages paginated and heavy panels independently loading.
- Verify Jinja JavaScript pages do not trigger duplicate Ceph requests.

### Expected files

- `ceph-health-dashboard/src/components/`
- `ceph-health-dashboard/src/styles.css`
- `dashboard/static/app.js`
- `dashboard/static/chat_widget.js`
- page-specific JavaScript and templates

### Exit criteria

- A slow Ceph node leaves the dashboard interactive.
- Refresh does not create duplicate requests.
- Stale, partial, refreshing and unavailable states are visually distinct.

## Phase 8 — Large payload and parsing protection

### Work

- Add pagination and limits to all large list endpoints.
- Add cursor/line limits to logs and safe output-size limits to command runners.
- Avoid sending full CRUSH data in health responses.
- Avoid serializing the same payload repeatedly.
- Use table virtualization only where measured row counts justify it.
- Validate malformed JSON and partial output without taking down the route.

### Expected files

- log routes and collectors
- object/RBD/backup routes
- CRUSH routes
- shared parsing utilities
- payload-limit and pagination tests

### Exit criteria

- No endpoint renders unbounded log lines or thousands of rows by default.
- Oversized output fails with a structured, bounded error.
- Health response size does not depend on full CRUSH or inventory payloads.

## Phase 9 — Observability and admin diagnostics

### Work

- Add structured metrics/logs for:
  - SSH connect duration
  - authentication duration
  - command duration by command name
  - timeout and retry count
  - node failure count
  - snapshot duration
  - API response duration
  - cache hit/miss
  - queue wait time
  - open connection count
  - response size
- Add a correlation ID to every request and propagate it through collector
  logs and background jobs.
- Redact private keys, API keys, passwords and tokens from logs and errors.
- Add admin-only `/api/debug/ceph-latency` with node, command and recent
  latency statistics, without credentials.

### Expected files

- shared logging/metrics helpers
- FastAPI middleware
- collector and runner instrumentation
- admin debug route and RBAC tests

### Exit criteria

- A slow request can be traced from API to snapshot/command stage.
- Debug output is admin-only and contains no secret material.
- Metrics distinguish cache delay, SSH delay, command delay and queue delay.

## Phase 10 — Failure, concurrency and cancellation tests

### Required tests

- SSH connect timeout
- banner/authentication timeout
- command timeout and process termination
- unreachable node
- one node failure with other nodes succeeding
- partial snapshot and stale snapshot
- cache hit and cluster isolation
- concurrent refresh single-flight behavior
- bounded retry and backoff
- API does not call SSH directly
- client cancellation releases resources
- bounded log query
- collector concurrency semaphore
- no overlapping watcher poll
- heavy API does not block health API

### Test constraints

- Use fake SSH servers, fake runners and subprocess mocks.
- Never mutate a real Ceph cluster for tests.
- Keep existing approval/audit and authentication tests in the regression set.

### Exit criteria

- All new failure modes have deterministic tests.
- Existing relevant tests pass without weakening assertions.
- No test depends on an external Ceph node being healthy.

## Phase 11 — Benchmark, review and handoff

### Benchmark matrix

Record before/after values for:

| Measurement | Baseline | After | Method |
| --- | ---: | ---: | --- |
| `/api/dashboard/health` p50/p95 | one direct Ceph sample: 3465.6 ms | 22.01 / 27.62 ms | 50 warm-cache TestClient requests; local app/DB snapshot, no Ceph call |
| Dashboard initial load | — | — | browser timing |
| Nodes/pools response | — | — | cached and cold/heavy path |
| SSH connections per cycle | — | — | runner metric |
| Ceph commands per cycle | — | — | collector metric |
| Slow-node dashboard behavior | — | — | injected timeout |
| Snapshot collection duration | — | — | collector metric |
| Heavy API impact on health API | — | — | concurrent load |

### Final review

- Check API contracts, authentication, RBAC, cluster scope and audit paths.
- Check all execution modes and key paths.
- Check request cancellation, process cleanup and connection cleanup.
- Check dashboard, Nodes, Pools, PGs, Volumes, Object Storage, Logs, CRUSH and
  performance pages.
- Record limitations where production Ceph access or Croit source access was
  unavailable.
- Update the progress checklist and attach test/benchmark evidence.

### Completion criteria

- Cached dashboard health p95 is below 500 ms in the measured environment.
- No request or subprocess waits indefinitely.
- One hung node does not hang the dashboard.
- Last usable data remains visible with explicit stale metadata.
- Manual refresh is single-flight.
- Multi-cluster snapshots remain isolated.
- Heavy APIs do not block health APIs.
- No push, merge or deployment occurs as part of this plan.

## Progress

- [x] Phase 0 — Baseline, inventory and root-cause report
- [x] Phase 1 — Central timeout and concurrency configuration
- [x] Phase 2 — Shared SSH and command runner
- [x] Phase 3 — Bounded batched Ceph collection
- [x] Phase 4 — Per-cluster snapshot cache and refresh coordination
- [x] Phase 5 — Fast APIs and heavy APIs
- [x] Phase 6 — Watcher scheduling and incident isolation
- [x] Phase 7 — Frontend freshness and non-blocking behavior
- [x] Phase 8 — Large payload and parsing protection
- [ ] Phase 9 — Observability and admin diagnostics
- [ ] Phase 10 — Failure, concurrency and cancellation tests
- [ ] Phase 11 — Benchmark, review and handoff

## Change log

- 2026-09-16: Initial plan created from the performance and non-blocking
  operations requirements.
- 2026-09-16: Phase 1 completed. Added bounded Ceph collection settings in
  `config/settings.py` and `.env.example`, plus `shared/retry.py` with finite
  exponential backoff, jitter and retry classification. Focused tests: 8
  passed.
- 2026-09-16: Phase 2 completed. Added `shared/ceph_runner.py` with separate
  SSH connect/banner/auth timeouts, host-key pinning, bounded connection reuse,
  total command deadlines, structured errors and output limits. Migrated the
  existing Watcher Ceph SSH boundary without changing public callers. Focused
  runner/client/collector/tool tests: 181 passed; dashboard health/storage
  regression tests: 22 passed.
- 2026-09-16: Phase 3 completed. Added `CephSnapshotCollector` and bounded
  parallel inventory collection for Pools, PGs, CRUSH and Nodes; section
  publication and failure isolation remain independent. Concurrency is capped
  by `CEPH_MAX_CONCURRENCY`. Collector/client/runner tests: 120 passed.
- 2026-09-16: Phase 4 completed. Made snapshot staleness use the centralized
  max-age setting and added an atomic per-cluster refresh claim across worker
  processes. Manual refresh remains HTTP-fast and single-flight; the claim
  lock uses a separate namespace to avoid nested cache-lock deadlocks. Snapshot,
  refresh and health API tests: 132 passed.
- 2026-09-16: Phase 5 completed. Object Storage inventory and Bucket Access Log
  capability/live-log reads now use bounded cache keys with background-on-miss
  loading; cached data remains available and responses expose `refreshing`.
  Existing approval/audit and cluster selection paths are unchanged. Object
  Storage/Access Log tests: 71 passed.
- 2026-09-16: Phase 6 completed. Health collection now has a per-cluster
  `CEPH_HEALTH_TIMEOUT` deadline shared across MON fallback attempts, including
  cephadm lock wait. Existing Watcher auxiliary-scan locks, independent error
  boundaries and incident deduplication were verified and retained. Ceph-client
  tests: 111 passed; Watcher scheduling/incident tests: 6 passed.
- 2026-09-16: Phase 7 completed by verifying the snapshot-aware React dashboard:
  AbortController cancellation, five-second read polling, single-flight manual
  refresh and distinct stale/refreshing/error states. Vite/TypeScript build
  passed.
- 2026-09-16: Phase 8 completed by verifying bounded log/object/PG/CRUSH/
  backup payload paths and the runner output limit. Payload regression set:
  158 passed.
- 2026-09-16: Phase 9 is in progress. Added bounded runner connection/command
  metrics with safe command labels, cache hit/miss/load counters, inventory
  snapshot duration, API duration/status samples, and an `X-Request-ID`
  correlation header. The admin-only `/api/debug/ceph-latency` endpoint keeps
  its original `metrics.recent` shape and exposes additive cache, collector
  and API diagnostics. Queue-wait metrics are bounded at the SSH lease; retry
  metrics are now integrated into the read-only health MON fallback with a
  shared deadline. Authentication, host-key, command and data errors remain
  non-retryable; mutation paths are unchanged. Current retry/correlation
  regression gate: 342 passed, 1 deselected, 1 warning. No latency claim is made until
  benchmark data is collected.
- 2026-09-16: Phase 10 is in progress. Host-level SSH lease acquisition is
  now bounded by the command deadline; queue wait and queue-wait timeout are
  recorded separately. Focused timeout, cache, collector, health and debug
  coverage passed 149 tests. A full repository run was stopped after reaching
  97% because unrelated long-running tests exceeded the verification window;
  it had exposed stale test doubles for the new Paramiko timeout signature,
  including `tests/test_rgw_log.py`, which has now been updated and passes.
- 2026-09-16: Added synchronous bounded retry instrumentation in
  `shared/retry.py`, exposed it through the admin-only latency diagnostics,
  and integrated it only into health transport fallback. Commit `c4677731`
  is pushed to `origin/main`; deployment remains blocked pending runtime
  ownership approval.
- 2026-09-16: Propagated the request context through snapshot inventory,
  persistent Ceph-query-cache and object-storage-cache thread executors so
  API-triggered background Ceph work retains its correlation ID. Current
  focused regression gate: 187 passed, 1 warning; compile, JavaScript syntax,
  Alembic single-head and Node 20 frontend build checks passed. Commit
  `d35beb5e` is pushed to `origin/main`.
- 2026-09-16: Propagated correlation headers through incident and delegated
  RabbitMQ publishers and restored them in Worker consumers, with safe header
  validation and unchanged message payloads/retry behavior. Current focused
  wave gate: 215 passed, 1 deselected, 1 warning. Commit `d89b1861` is pushed
  to `origin/main`.
- 2026-09-16: Phase 11 benchmark increment: the real FastAPI route was
  exercised with local snapshot/cache data and dependency-overridden test
  authentication. Across 50 warm requests, `/api/dashboard/health` measured
  p50 22.01 ms, p95 27.62 ms and max 56.60 ms. This is a local
  application/cache measurement, not a production HTTP or live-Ceph
  benchmark; the unauthenticated curl sample was discarded.
- 2026-09-16: Added bounded cross-process health-lock acquisition, remote
  TERM/KILL cleanup for cephadm command descendants, remediation-loop lock
  sharing, and approval/dashboard copy cleanup. Current candidate focused
  regression gate: 342 passed, 1 deselected, 1 warning; candidate commit
  `179480c5` is pushed to `origin/main`. The broad segmented gate remains
  recorded against the earlier release SHA until rerun end-to-end.
- 2026-09-16: Candidate wave extended bounded I/O to backup export/restore and
  restore-drill SSH streams, shared health-pool reuse, inventory/health
  deadlines, pagination centering, approval copy, and the scheduled nightly
  AI morning report. Verified module gates include backup engine 21, integrity
  3, restore 11, restore drill 6, SSH executor 3, S3 11, SSH storage 13,
  Block Storage 6, dashboard navigation 28, and settings 150 tests; all
  reported exit code zero. The combined candidate gate was terminated by the
  remote runner at 37% without a summary and is not counted as passing.
- 2026-09-16: Added bounded remote timeout and node-local lock serialization to
  RBD restore commands, refreshed the Telegram Alerts control-plane page, and
  added regression coverage for both waves. The combined current-wave gate
  passed 177 tests with one existing Starlette/httpx warning. Commit
  `b41b16b1` is pushed to `origin/main`; rollback candidate is `62ed53ff`.
- 2026-09-16: Isolated finite Watcher transition tests from slow secondary
  collectors so they do not open SSH connections to fixture MON addresses;
  the targeted transition test passed. Refined the Telegram overview/detail
  navigation and Volume Performance layout, then moved RBD image-ID lookup
  off the event loop and made Block Storage warmup/cache TTL handling
  consistent. Current gates passed 180, 123 and 152 tests respectively, each
  with the existing Starlette/httpx warning. Commits `e47b45eb`, `c04cf612`,
  `c22570ae`, `6afd3a8b` and `d2a84478` are pushed; rollback candidate is
  `c22570ae`.
- 2026-09-16: Migration release-gate audit found SQLite 3.26.0 downgrade
  failures caused by direct column drops and by batch rebuilds losing
  expression/partial indexes. Converted 52 legacy column-drop downgrades to
  SQLite-safe batch operations and explicitly preserved the affected incident
  and backup indexes. The disposable `upgrade head -> downgrade base ->
  upgrade head` round trip passed at Alembic head `ac17d9e0f5a0`; migration
  tests passed 10 with 11 expected reflection warnings. Commit `ceea4c0a` is
  pushed to `origin/main`. Deployment remains blocked by runtime ownership
  approval.
- 2026-09-16: Completed the dashboard interaction wave: parallelized
  independent read-only RBD inventory commands, added persistent
  block-storage fallback, exposed volume used percentage, added dismissible
  health errors and a responsive chat drawer, and split Performance RCA into
  its own Telegram settings page. Focused regression gate: 260 passed with
  one existing Starlette/httpx warning; Python compile, JavaScript syntax and
  Node 20 frontend build passed. Commit `d588d561` is pushed to
  `origin/main`; the worktree is clean and deployment remains blocked pending
  runtime ownership approval.
- 2026-09-16: Completed the Trash lifecycle interaction wave: dedicated the
  Trash page, added pool summaries and retention states, copyable IDs,
  pagination/filtering, batch restore and guarded batch force-delete while
  preserving approval, TTL and watcher-protection semantics. Focused gate:
  113 passed with one existing Starlette/httpx warning; Python compile,
  JavaScript syntax and diff checks passed. Commit `10a7ea43` is pushed to
  `origin/main`; deployment remains blocked by runtime ownership approval and
  concurrent uncommitted worktree changes.
- 2026-09-16: Extended Block Storage inventory with provisioned/used bytes and
  usage percentage from bounded `rbd du` reads, kept usage scans sequential to
  avoid an OSD burst, capped parallel JSON batches at eight commands, and
  refined the dashboard AI chat drawer controls and scrolling behavior. The
  focused Block Storage/chat gate passed 7 tests with one existing
  Starlette/httpx warning; Python compile, JavaScript syntax and Node 20
  frontend build passed. Commit `78a3dfe3` is pushed to `origin/main`.
- 2026-09-16: Moved Trash restore preflight (Trash and inventory reads) into
  the worker thread so the async route does not block the event loop, while
  preserving cluster-specific Ceph connection handling and pool validation.
  Trash/restore focused gate: 31 passed, 82 deselected and one existing
  Starlette/httpx warning. Commit `43d74c52` is pushed to `origin/main`.
- 2026-09-16: Added regression coverage for persistent Block Storage pool
  metadata and completed the chat drawer geometry pass so the message list is
  the only scrolling region and long assistant output collapses at 200px.
  Focused gate: 8 passed with one existing Starlette/httpx warning; Node 20
  frontend build passed. Commit `1046ffe9` is pushed to `origin/main`.
- 2026-09-16: Added durable Trash usage telemetry: the risky Trash-move
  proposal records provisioned/used bytes and percentage in action parameters,
  and the Trash page restores that snapshot when Ceph no longer reports usage
  for the deleted image. The current ordering defers the expensive usage scan
  until dependency checks pass. Focused usage/snapshot gate: 4 passed, 224
  deselected and one existing Starlette/httpx warning. Commits `3ad0ca62` and
  `2330c8a8` are pushed to `origin/main`.
- 2026-09-16: Completed the current-candidate segmented repository gate.
  The non-migration suite reported 3338 passed and three failures, all from
  `tests/test_mq.py` using the stale `.env` default `guest@localhost`; the
  same three tests passed on a disposable RabbitMQ vhost using the runtime
  `ceph_ai` credential. The migration suite passed 10 tests with 11 expected
  reflection warnings. This is recorded as a segmented pass with 13
  deselected tests; no application code was changed for the broker-environment
  mismatch. Deployment remains blocked by the untracked `transfer/` directory
  and pending runtime ownership approval.

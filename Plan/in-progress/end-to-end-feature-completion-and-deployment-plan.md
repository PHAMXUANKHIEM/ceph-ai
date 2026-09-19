# End-to-End Feature Completion and Deployment Plan

**Project:** `ceph-ai`
**Target:** `10.3.55.213:/root/ceph-ai`
**Created:** 2026-09-14
**Owner:** Engineering + Operations

## 1. Objective

Complete, verify, release, and deploy the remaining `ceph-ai` features as
production-ready vertical slices. Every slice must include backend/service
logic, API, UI, RBAC, auditability, tests, documentation, observability, and a
rollback path.

This plan covers:

- Realtime Ceph cluster status and snapshot delivery.
- Block storage and RBD lifecycle.
- Backup, restore, and disaster recovery.
- Object storage/RGW operations and observability.
- AI forecasting, diagnosis, recommendations, remediation, and guarded
  autopilot.
- Security, operations, release gates, and deployment.

This plan does not authorize enabling destructive automation or the watcher in
production. Those actions require a separate operator decision after the
relevant gates pass.

## 2. Current baseline

- At the latest review, `main` and `origin/main` are equal and the
  release-candidate worktree is clean after the reviewed feature, observability,
  and evidence commits. Classification is recorded in
  `docs/ai/end-to-end-change-classification.md`.
- The systemd Dashboard, Worker, and Watcher units are disabled/inactive, but
  independently managed Podman containers are running, including a healthy
  Watcher. This runtime ownership conflict must be resolved by an operator
  before any restart or rollout.
- The deterministic release command and the focused hardening gate pass after
  fixing the admin Ceph latency debug return path. The isolated Alert Center
  unique-index regression tests pass (`2 passed, 35 deselected`).
- Python compilation, `git diff --check`, the Node 20 frontend production
  build, and the single Alembic-head check pass for the current worktree.
- RabbitMQ credentials are valid for the runtime `ceph_ai` user. The minimal
  production permission update now allows writes through `amq.default`.
  Integration tests pass in an isolated temporary vhost (`3 passed`); direct
  testing on `/` is not meaningful while the live Worker/Watcher consumers
  drain the shared `incidents` queue.
- The runtime configuration currently has global autonomous flags enabled, but
  both configured clusters have per-cluster autopilot disabled. This requires
  explicit review before release or restart.
- Existing deployment entry point:
  `bash scripts/deploy/restart_services.sh`.

The next work items are the remaining live-browser, deployment-approval, and
operational-readiness gates; code and evidence changes must remain reviewable
and pushed before deployment is considered.

## 3. Non-negotiable engineering rules

- No feature is marked complete from UI or prompt work alone.
- All cluster reads and writes are scoped by `cluster_id`.
- Reads use bounded timeouts, stale-data metadata, and fail-closed capability
  checks.
- Writes use typed action IDs, RBAC, preview, confirmation, approval where
  required, idempotency, audit, timeout, post-check, and rollback.
- AI receives redacted evidence only; it cannot execute free-form shell commands.
- Destructive actions remain disabled until their policy and failure tests pass.
- No deployment starts with unexplained dirty files, failing release-gate tests,
  unapplied migrations, or an unknown rollback procedure.
- Every release records commit SHA, migration head, configuration changes,
  enabled flags, test results, and operator approval.

## 4. Delivery phases

### Phase 0 — Stabilize the repository and deployment foundation

- [x] Review and classify every current uncommitted change. Evidence:
  `docs/ai/end-to-end-change-classification.md`.
- [x] Separate unrelated changes into isolated commits. The release
  foundation, Natural Language slice, and conversation regression are in
  separate commits.
- [x] Push the reviewed commit series only after release-gate tests. `main` and
  `origin/main` are equal after the full suite, focused gates, migration
  round-trip, hardening gate, and Node 20 build passed.
- [x] Reproduce and fix the Alert Center unique-index test failure. The
  suspected unique-index regression was not reproducible; the focused
  concurrency/unique-index tests pass (`2 passed, 35 deselected`). The full
  run exposed and fixed a separate admin Ceph latency debug return-path bug,
  covered by `tests/test_ceph_debug.py` (`4 passed`).
- [x] Run the full Python suite from `.venv/bin/pytest`; record failures by
  category instead of skipping them. Evidence:
  `.venv/bin/pytest -q -k 'not live and not integration'` → `3817 passed,
  47 deselected, 235 warnings` in `1742.85s`; RC=0. The only earlier failure
  was the admin Ceph latency debug return path, fixed and covered by
  `tests/test_ceph_debug.py` (`4 passed`).
- [x] Use Node 20 for all frontend type-check and production builds. Evidence:
  `/opt/ceph-ai-node20/bin/node` and the successful `npm run build`.
- [x] Verify one Alembic head and test upgrade plus downgrade on a disposable DB.
  Evidence: `alembic heads`/`alembic current` report
  `m20260919nlcontext (head)`; disposable SQLite run completed
  `upgrade=0 downgrade=0 reupgrade=0`. The legacy `b5c6d7e8f9a0` downgrade
  was made SQLite-compatible with Alembic batch operations.
- [x] Inventory secret-file and runtime ownership permissions without exposing
  secret values. Evidence: `/root/ceph-ai/.env` is `0600 root:root`; systemd
  Dashboard/Worker/Watcher units are disabled/inactive; independently managed
  Podman Dashboard/Worker/Watcher containers are healthy.
- [ ] Resolve the backup location and service-account ownership policy before
  deployment. `/var/backups/ceph-ai` is `0700 root:root`, while the documented
  `/var/lib/ceph-ai/backups` path is absent; runtime ownership also remains
  split between systemd and Podman.
- [ ] Define staging/canary/production cluster IDs and ensure test data cannot
  reach production.
- [x] Add a release manifest containing commit SHA, migration revision, image
  versions, feature flags, and rollback SHA. Evidence:
  `docs/ai/end-to-end-release-manifest.md`.

**Exit gate:** clean release branch, full release-gate tests pass, migrations
are reversible, and the deployment manifest is reviewable.

### Phase 1 — Finish the realtime snapshot foundation

Scope: RT-00 through RT-04 in
`Plan/realtime-cluster-status-update-plan.md`.

- [ ] Complete multi-tab baseline measurements for 1, 5, and 10 browser tabs.
- [x] Finish snapshot lifecycle APIs:
  `mark_refreshing`, `is_refreshing`, and `invalidate_snapshot`. Evidence:
  `tests/test_cluster_snapshot.py` and `tests/test_nl_snapshot_runner.py`.
- [x] Complete cross-process refresh locking and checksum validation. Evidence:
  snapshot process/lock regression tests and persistent versioned cache tests.
- [x] Add collector success/failure counters and per-command metrics. Evidence:
  `tests/test_cluster_snapshot_collector.py`.
- [x] Verify snapshot persistence across Watcher restart and stale UI behavior.
  Evidence: `tests/test_cache_warmup.py`, `tests/test_cluster_snapshot.py`,
  and the dashboard snapshot API tests.
- [x] Keep the Watcher disabled during tests that could send real alerts. The
  release run excludes `live` and `integration` tests; no live alert-producing
  test is used in this gate.
- [x] Commit the current health snapshot, warmup, collector, and dashboard
  slice. The current realtime hardening is in pushed commit `f40d084c` and
  post-check invalidation is in `0be87d8a`.
- [x] Record server-side test evidence and query-rate comparison against the
  baseline. Evidence:
  `docs/ai/realtime-snapshot-load-evidence-2026-09-19.md` (1/5/10 simulated
  tabs, p95 under 2 ms, zero Ceph/SSH queries versus the old live-query path).
- [ ] Complete the corresponding real browser 1/5/10-tab run with DevTools,
  authentication, and canary scope.

**Exit gate:** health reads do not perform live Ceph queries, refresh is
single-flight per cluster, old data remains visible with a stale label, and
restart does not create duplicate collectors.

### Phase 2 — Complete realtime dashboard migration

Scope: RT-05 through RT-09.

- [x] Implement snapshot read APIs for Pools, PGs, CRUSH, and Nodes. Evidence:
  `tests/test_dashboard_pgs.py`, `tests/test_dashboard_crush_map.py`, and
  `tests/test_dashboard_nodes.py`.
- [x] Preserve existing response fields and add freshness metadata. The
  focused API gate passed `47 passed, 1 warning`.
- [x] Move filtering, sorting, and pagination onto snapshot data for the
  migrated dashboard sections; the focused API/UI gate passed `47 passed`.
- [x] Add a shared frontend snapshot/freshness hook and status component. The
  Node 20 build includes `useClusterSnapshotEvents`, shared snapshot state,
  and status labels for the migrated React pages.
- [x] Remove page reloads and independent polling loops from the migrated
  health, Pools, PGs, CRUSH, and Nodes snapshot views. They now share one
  cluster-state WebSocket and re-read only the invalidated API section; HTTP
  polling remains bounded fallback behavior. Evidence: commit `38ecab34`,
  Node20 syntax checks, and navigation/WebSocket gate (`47 passed, 1 warning`).
- [ ] Migrate remaining mutation/progress and non-snapshot pages away from
  intentional post-action reloads or page-local polling where an equivalent
  event contract exists.
- [x] Add WebSocket or SSE invalidation events with HTTP polling fallback.
  Evidence: `tests/test_dashboard_ws.py` and Node 20 build.
- [x] Enforce HTTP-equivalent authentication and cluster isolation for events.
  Evidence: scoped WebSocket tests in `tests/test_dashboard_ws.py`.
- [x] Publish events only after snapshot commit or mutation post-check. Snapshot
  events are published after versioned cache commit; resolved-incident
  invalidations are published after the database commit that records the
  post-check-confirmed `RESOLVED` state. Evidence: `tests/test_dashboard_ws.py`
  (`9 passed`).
- [x] Add mutation-to-invalidation mapping for pool, CRUSH, OSD, RGW, node, and
  deployment changes. The committed post-check hook maps resolved incident
  codes to bounded snapshot sections and keeps RGW changes on the status hint
  without sending object data through the event channel. Evidence:
  `tests/test_dashboard_ws.py` (`12 passed`).
- [ ] Add bounded retries, circuit breakers, concurrency limits, correlation
  IDs, stale alerts, cache-size monitoring, and event reconnect metrics.
  Current admin debug evidence now includes collector/cache/API/retry and
  WebSocket connection/message/disconnect/policy counters; the remaining
  operator dashboards, stale/dead-collector alerts, and browser reconnect
  telemetry are still open.
- [ ] Execute the RT-05 through RT-09 exit-gate browser and load tests.

**Exit gate:** changing dashboard sections does not open SSH connections,
cluster events never cross tenant or cluster boundaries, and p95/query-rate
targets are proven with metrics.

### Phase 3 — Complete block-storage and RBD features

Scope: BS-01 through BS-09 in
`Plan/block-storage-roadmap.md`.

#### BS-01/02/03 — Inventory, volume lifecycle, and attachment

- [x] Finish authoritative RBD inventory: size, used space, features, watcher,
  lock, snapshots, parent/child, and cluster/pool filtering. Evidence:
  deterministic block/RBD gate (`276 passed`).
- [x] Finish create, expand-only resize, rename, trash, and restore workflows.
  Evidence: volume lifecycle, trash, restore and dashboard volume tests in the
  same `276 passed` gate.
- [ ] Finish Cinder discovery and watcher/consumer mapping.
- [x] Route every mutation through the Worker and action allowlist. Evidence:
  block-storage policy, dashboard action, and executor regression tests.
- [x] Add idempotency, reconciliation, capacity/dependency preflight, and
  post-checks. Evidence: RBD reconciliation, dependency, capacity, integrity,
  and policy tests.
- [x] Block delete, force-detach, and unlock when dependencies or policy forbid
  the operation. Evidence: dependency and trash lifecycle tests.

#### BS-04/05 — Snapshot, clone, restore, and protection

- [x] Implement snapshot schedules, retention, timezone handling, and dedup.
  Evidence: `tests/test_volume_snapshot_policy.py` and scheduler tests.
- [x] Implement restore-as-new by default and guarded in-place rollback.
  Evidence: dashboard volume and restore preflight tests.
- [x] Implement clone dependency graph, flatten preflight, and protected
  snapshot handling. Evidence: dependency and volume lifecycle tests.
- [ ] Complete recovery-point selection, incremental-chain validation,
  checksum/size verification, and restore promotion approval.

#### BS-06/07/08 — Performance, Cinder, and DR

- [x] Collect throughput, queue depth, percentile latency, physical/logical
  capacity, and freshness. Evidence: volume performance, capacity, and
  monitoring tests.
- [x] Implement QoS templates, diff, rollback, and unsupported-capability
  fail-closed behavior. Evidence: volume performance/policy tests.
- [ ] Complete OpenStack Cinder volume/project/instance/attachment mapping and
  orphan reporting.
- [ ] Implement RBD mirroring inventory, lag/RPO, planned failover/failback,
  fencing, split-brain protection, and non-production DR drills.

#### BS-09 — AI storage intelligence

- [x] Add evidence-backed stale/unattached/waste insight. Evidence:
  block-storage insight and dependency tests.
- [x] Add volume-to-pool-to-OSD performance diagnosis. Evidence: volume
  performance analysis and monitoring tests.
- [ ] Add recommendation simulation for resize, QoS, flatten, retention, and
  placement.
- [x] Keep all recommendations read-only until the shared action policy and
  post-check contract are complete. Evidence: policy/preflight gates and the
  full deterministic suite; no automatic storage remediation was enabled.

**Exit gate:** default, secondary, inactive, degraded, full, locked, and
Cinder-managed scenarios pass without cross-cluster access or mutation bypass.

### Phase 4 — Complete backup and disaster recovery

Scope: `Plan/backup-roadmap.md`.

- [ ] Audit all backup, deduplication, in-flight, retention, and audit queries
  for multi-cluster isolation.
- [ ] Remove default-cluster-only behavior from Backup Digest and Restore Drill.
- [ ] Finish Recovery Point, Target, and Policy workspaces in the Dashboard.
- [ ] Complete restore verification, dependency-chain display, and stale/failed
  target handling.
- [x] Test concurrent backup/restore, retry idempotency, immutable targets,
  insufficient capacity, checksum failure, and partial chain failure. Evidence:
  deterministic backup/restore gate (`218 passed, 1 warning`), including engine,
  integrity, storage, restore, drill, and Dashboard backup tests.
- [ ] Complete cluster/site DR runbook and evidence capture.

**Exit gate:** every cluster has independently scoped backup history, digest,
restore drill, retention, audit, and recovery evidence.

### Phase 5 — Complete object storage and RGW features

Scope: `Plan/object-storage-roadmap.md`.

- [x] Finish S3 user/access-key regression coverage, including rollback and
  audit failure cases. Evidence: deterministic object/RGW gate (`216 passed,
  1 warning`) covering Dashboard user/key, policy, audit, cache, S3 and RGW
  connectivity/diagnosis paths.
- [ ] Implement object version delete/restore with confirmation, policy checks,
  Object Lock handling, and audit.
- [ ] Add bounded RGW, bucket, and user metrics: requests, bytes, errors,
  latency, and quota.
- [ ] Add trend dashboards, top-consumer views, CSV/JSON reports, and secret
  redaction.
- [ ] Add deduplicated/resolvable alerts for quota, 5xx, access denied, hot
  bucket, and anomalous access.
- [ ] Implement RGW service overview, endpoint reachability, frontend/version,
  realm/zone/zonegroup, sync lag, and read-only diagnostics.
- [ ] Add controlled remediation only through the shared action policy.
- [ ] Complete RBAC, CSRF/rate-limit review, audit viewer, runbooks, API docs,
  release matrix, and navigation updates.

**Exit gate:** object operations work on supported Ceph versions, unsupported
capabilities fail closed, large listings remain bounded, and no S3 secret is
stored or logged.

### Phase 6 — Complete AI intelligence and cost controls

Scope: `Plan/ai-missing-features-roadmap.md` and
`Plan/ai-cost-optimization-plan.md`.

#### Read-only intelligence

- [ ] Finish operator-maintained, source-backed capability-matrix entries.
- [ ] Finish AI preflight validation for action, target, version, health, and
  dependencies.
- [ ] Implement capacity time-series collection, forecasting, confidence
  intervals, backtesting, and threshold alerts.
- [ ] Implement block-storage inventory insight and protection-gap analysis.
- [ ] Implement performance correlation, hot-resource detection, and
  recommendation simulation.
- [ ] Implement RGW evidence collection, bucket diagnosis, multisite diagnosis,
  security insight, and audit-log intelligence.
- [ ] Implement Pool/PG, CRUSH placement, scrub, and inconsistent-object
  analysis.
- [ ] Implement unified incident timeline, correlation chain, evidence-backed
  postmortem, review, and export.
- [ ] Implement the capacity planner with workload model, topology scenarios,
  comparison, explainability, and export.

#### Controlled actions and autonomy

- [ ] Implement the shared remediation state machine.
- [ ] Implement the universal post-check and rollback contracts.
- [ ] Implement only typed, allowlisted RGW/Block/Vitastor actions.
- [ ] Add failure injection, stale-evidence, timeout, duplicate, and rollback
  tests.
- [ ] Add shadow mode, approval mode, and guarded auto mode.
- [x] Keep auto mode disabled by default and require explicit per-cluster
  promotion. Evidence: autonomy/settings/router guard tests in the full
  deterministic suite and AI gate (`248 passed`); no autopilot promotion was
  performed.

#### Cost and provider controls

- [ ] Reduce provider round trips through same-turn read-only caching, early
  stopping, and per-feature output limits.
- [ ] Add controlled model routing with canary quality comparison, rollback, and
  budget protection.
- [x] Verify telemetry is content-free and never stores prompts, responses, or
  credentials. Evidence: AI observability, cost, routing, security, and
  Natural Language redaction tests in the AI gate (`248 passed`).

**Exit gate:** every AI conclusion links to current evidence, insufficient
evidence produces `INSUFFICIENT_EVIDENCE`, and no AI path can bypass RBAC or
the action executor.

### Phase 7 — Cross-cutting hardening and operational readiness

- [x] Verify RBAC, CSRF, rate limits, validation, secret redaction, and
  tenant/product authorization tests. Evidence: the deterministic suite plus
  the hardening gate (`110 passed, 1 warning`) and AI security gate (`248
  passed, 1 warning`).
- [ ] Complete security review for encryption, key rotation, and production
  tenant-isolation sign-off.
- [x] Verify the audit viewer, audit filtering, mutation metadata, and
  runbook-evidence validation paths. Evidence: hardening gate (`110 passed, 1
  warning`).
- [ ] Complete audit retention/export policy and operator sign-off.
- [ ] Add operator dashboards for API/job latency, queue depth, collector lag,
  stale age, provider usage, action outcomes, and service health. Backend
  collector/API/WebSocket counters are instrumented, but the operator-facing
  dashboard is not complete.
- [ ] Add alerts for stale snapshots, dead collectors, failed post-checks,
  budget exhaustion, backup gaps, and event-bus failure.
- [ ] Complete runbooks for deploy, rollback, Ceph outage, stale data, backup
  restore, RGW failure, Cinder dependency, DR failover, and credential loss.
- [x] Add deterministic health and smoke coverage for Dashboard APIs, service
  heartbeats, frontend navigation/assets, and stale/dead process handling.
  Evidence: hardening gate (`110 passed, 1 warning`).
- [ ] Run live production-like health checks and smoke tests for Dashboard,
  Worker, Watcher, database, cache, message broker, and frontend assets.
- [x] Document supported Ceph releases and fail-closed behavior for unsupported
  releases. Evidence: `docs/ai/ceph-official-docs-manifest.md` lists Reef 18,
  Squid 19, and Tentacle 20 with version-filtered official sources; capability
  inventory/matrix and preflight tests reject unknown, mixed, and uncovered
  releases.
- [ ] Add retention and disk-growth controls for snapshots, logs, audit records,
  and generated reports.

**Exit gate:** Operations can identify the failing layer from metrics and can
recover the service without manual source edits.

## 5. Test and release gates

Each feature slice must pass all applicable gates:

- [x] Unit and regression tests. Evidence: deterministic suite (`3817 passed,
  47 deselected, 235 warnings`; RC=0).
- [x] API contract and schema tests. Evidence: deterministic API/dashboard
  gates and the full release suite passed.
- [x] Frontend type-check and production build with Node 20.
- [x] RBAC, cluster-scope, CSRF, secret-redaction, and prompt-injection tests.
  Evidence: deterministic suite, AI gate, and hardening gate passed.
- [x] Timeout, retry, stale-data, partial-data, and backend-unavailable tests.
  Evidence: deterministic suite and realtime/storage focused gates passed.
- [x] Migration upgrade/downgrade and restart-recovery tests. Evidence: the
  disposable SQLite round-trip passed; restart/stale-state coverage passed in
  the deterministic suite.
- [ ] Default, secondary, inactive, and mixed-version cluster tests.
- [x] Mutation preview, approval, idempotency, audit, post-check, and rollback
  tests. Evidence: block/RBD, backup/restore, object/RGW, and AI focused gates
  passed.
- [ ] Browser smoke tests and multi-tab load tests.
- [x] Backup/restore evidence where applicable. Evidence: deterministic backup
  gate (`218 passed, 1 warning`).
- [ ] Live DR drill evidence.
- [x] Full release suite completes with no unexplained failures. Evidence:
  `3817 passed, 47 deselected, 235 warnings`; RC=0.
- [ ] Security and operations review is recorded.

## 6. Deployment waves

### Wave A — Foundation and read-only safety

Deploy:

- Phase 0 fixes.
- Realtime snapshot health path.
- Block/object inventory read-only paths.
- Backup multi-cluster scoping.
- Capability and audit improvements.

Configuration:

- Keep Watcher disabled until live polling is explicitly approved.
- Keep all destructive and autonomous actions disabled.
- Enable detailed metrics and error logging with secret redaction.

Validation:

- Run migration check.
- Run focused tests and complete release suite.
- Build frontend with Node 20.
- Run `bash scripts/deploy/restart_services.sh`.
- Verify Dashboard, `/pgs`, health APIs, snapshot freshness, logs, and service
  status.
- Observe one canary cluster before expanding scope.

### Wave B — Controlled storage operations

Deploy:

- Volume CRUD, snapshots, restore-as-new, object operations, backup workspaces,
  and approved QoS/reporting features.

Configuration:

- Require admin approval for every write.
- Keep destructive purge, in-place restore, force-detach, unlock, and DR
  failover disabled unless individually approved.

Validation:

- Execute only disposable-cluster scenarios first.
- Confirm audit records, post-checks, idempotency, rollback, and alert behavior.
- Verify no action crosses cluster or tenant scope.

### Wave C — AI read-only intelligence

Deploy:

- Forecasting, diagnosis, evidence reports, postmortems, capacity planner,
  and cost controls.

Configuration:

- Enable read-only AI and budget guard.
- Use shadow mode for model routing.
- Reject insufficient or stale evidence.

Validation:

- Compare AI results with known test incidents and backtests.
- Verify evidence links, redaction, cost telemetry, and quality thresholds.
- Do not enable automated remediation.

### Wave D — Guarded remediation

Deploy only after Wave C is stable:

- Typed remediation actions, post-checks, rollback planner, and operator
  approval workflow.

Configuration:

- Per-cluster allowlist.
- Short approval TTL.
- Concurrency and rate limits.
- Automatic rollback where technically safe.
- Autopilot remains disabled.

Validation:

- Failure-injection tests on a disposable cluster.
- Verify queued/running/verifying/succeeded/failed state transitions.
- Verify stale snapshots remain available after action failure.

### Wave E — Optional guarded autopilot

This wave requires written approval.

- [ ] Define SAFE actions and measurable post-checks.
- [ ] Define per-cluster blast-radius limits and maintenance windows.
- [ ] Run shadow mode for a complete observation period.
- [ ] Promote one disposable or canary cluster.
- [ ] Review every automatic action and rollback.
- [ ] Expand only after the agreed error and rollback thresholds are met.

## 7. Deployment procedure

1. Confirm the target commit, clean worktree, migration revision, feature flags,
   and release manifest.
2. Create or verify a database backup and configuration backup.
3. Run the release suite and frontend build in the same environment used for
   deployment.
4. Pull the exact approved commit on the server.
5. Apply migrations with a reviewed command and verify the single Alembic head.
6. Run `bash scripts/deploy/restart_services.sh`.
7. Verify Dashboard, Worker, Watcher, database, cache, broker, and frontend
   asset health.
8. Run API and browser smoke tests, including `/pgs`.
9. Validate snapshot generation, freshness, cluster isolation, and audit writes.
10. Monitor logs and metrics during the canary window.
11. Record deployment evidence and operator approval before expanding scope.

## 8. Rollback procedure

Trigger rollback on failed smoke checks, migration mismatch, cross-cluster
leak, secret exposure, duplicate collector, incorrect mutation, failed
post-check, unacceptable latency, or alert flood.

1. Disable new feature flags and stop autonomous actions.
2. Stop or disable the Watcher if it is producing unsafe polling or alerts.
3. Preserve logs, audit records, snapshots, metrics, and the failed release
   manifest.
4. Revert services to the last approved application commit.
5. Run `bash scripts/deploy/restart_services.sh`.
6. Roll back the database only when the migration is explicitly reversible and
   the data-impact review approves it.
7. Verify read-only health, cluster isolation, service status, and alert
   silence.
8. Document the incident, root cause, recovery evidence, and corrective action.

## 9. Definition of Done

The entire program is complete only when:

- [ ] All planned work packages are either complete or explicitly deferred with
  an owner and reason.
- [ ] Every deployed feature has code, API, UI, RBAC, audit, tests, docs, and
  rollback evidence.
- [ ] The full release suite completes successfully.
- [ ] Frontend build succeeds with Node 20.
- [ ] All migrations are applied and recoverable.
- [ ] Realtime p95 and query-rate targets are proven in a multi-tab test.
- [ ] Backup restore and DR drills have current evidence.
- [ ] AI outputs are evidence-backed, redacted, budget-controlled, and
  fail-closed.
- [ ] Destructive actions require policy and approval.
- [ ] Autopilot is disabled unless separately approved.
- [ ] Deployment and rollback have been rehearsed on a non-production target.
- [ ] The final release manifest, runbooks, test results, and operator sign-off
  are stored in the repository.

## 10. Tracking and handoff

For each completed item, update the source roadmap with:

- Date and work-package ID.
- Summary of code and configuration changes.
- Exact test commands and results.
- Commit SHA and deployment wave.
- Evidence location.
- Remaining risks, owner, and next action.

The next immediate actions are Phase 0, then Phase 1. Do not start a later
deployment wave while the preceding exit gate remains open.

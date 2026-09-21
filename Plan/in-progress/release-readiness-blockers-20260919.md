# Release Readiness Blockers — 2026-09-19

**Project:** `ceph-ai`  
**Repository:** `/root/ceph-ai` on `10.3.55.213`  
**Status:** Incomplete — RR-01/RR-02 graph cleanup, RR-03 forecast-event compatibility, RR-04 learning controls, RR-05 watcher post-commit alert delivery, specialized watcher producers, and the backup worker legacy managed-channel/AI path are implemented and tested. RR-06 deterministic collection is implemented and tested; staging, live smoke/rollback, final security review, and operator approval remain pending.
**Priority:** P0 / production gate  
**Default operating mode:** advisory or approval-required; autonomous remediation remains disabled

## 1. Objective

Bring the current branch to a reproducible release state after the latest
production-readiness review. The work is complete only when:

- Alembic has exactly one migration head and both empty-database and
  production-database upgrade paths are verified.
- The complete pytest collection succeeds without import errors or accidental
  collection of `transfer/` tests.
- Node-health incidents are committed before Telegram delivery is attempted.
- Online-learning controls, models, migrations, and tests use one consistent
  contract.
- Temporary backups and schema dumps are removed from Git and ignored.
- UI tests match the current navigation contract.
- A release manifest records the commit, migration head, tests, flags,
  rollback point, and operator approval.

This plan does not authorize production deployment or enabling autonomous
remediation.

## 2. Verified baseline

Reconciled on 2026-09-21 against the server, repository, and current release
manifest. The previous 2026-09-19 dirty-tree/two-head snapshot below is
historical and must not be used as the current state.

- `main` and `origin/main` are equal; the worktree is clean.
- `.venv/bin/alembic heads` reports exactly one code head: `m20260921rgwmetrics`.
  The connected database currently reports `m20260921tgoutbox`, so the new RGW
  metrics migration is intentionally still unapplied; no live upgrade was run.
- `pyproject.toml` now scopes the default collection to `tests/` and excludes
  `live`/`integration` by default.
- The latest collection gate records `3934/3950` tests under `tests/`, with 16
  live/integration tests deselected. All 3934 collected default test nodes pass
  when executed in four deterministic tmpfs shards; the single-process run remains
  a performance/environment issue and is not used as migration approval evidence.
- The release manifest is `docs/ai/end-to-end-release-manifest.md`; deployment
  remains unapproved and autonomous remediation remains disabled.

Current blockers:

| ID | Current status | Evidence / remaining work | Severity |
| --- | --- | --- | --- |
| RR-01 | Resolved | One code head: `m20260921rgwmetrics`; the connected database remains at `m20260921tgoutbox` until the pending staging/production rehearsal. | P0 |
| RR-02 | Resolved | Duplicate orphan branches were removed from the current graph; final compatibility review is still recorded as a release task. | P0 |
| RR-03 | Resolved | `NodeResourceForecastAlertEvent` model/migration and fallback tests are present and covered by the forecast gate. | P0 |
| RR-04 | Resolved | `is_paused()` contract and fail-closed learning-control tests are present. | P1 |
| RR-05 | Resolved | Incident, periodic health, forecast, RCA, log-intelligence, Vitastor, Vault, Trash, replay, metrics, verification, recovery, and backup-worker delivery use the durable outbox; credentials are resolved only at delivery time. | P1 |
| RR-06 | Resolved | `testpaths=["tests"]` and explicit `live`/`integration` markers are configured; deterministic suite passes. | P1 |
| RR-07 | Mostly resolved | Tracked backup/schema artifacts were removed and ignored; final secret/image-context scan remains open. | P1 |
| RR-08 | Mostly resolved | Navigation contract tests and volume-detail active-state regression pass; browser smoke remains open. | P2 |

## 3. Rules for implementation

- Preserve the current production safety posture: no autonomous remediation,
  no destructive command execution, and no secret values in plans or logs.
- Work on a dedicated branch or checkpoint before changing migrations.
- Never edit an applied migration in place. Add a corrective migration or a
  deliberate merge revision.
- Do not resolve duplicate migrations by deleting files until the live
  database revision and schema have been inspected.
- Every database change must have an upgrade test, a downgrade/rollback plan,
  and evidence from a disposable PostgreSQL database.
- Keep test fixes and production fixes in the same contract; do not weaken a
  test merely to make collection pass.
- Do not remove artifacts until they are confirmed to contain no required
  recovery evidence. If evidence is needed, move it to a controlled artifact
  store outside the application repository.

## 4. Workstream RR-01/RR-02 — consolidate the Alembic graph

### 4.1 Inventory the graph

- [ ] Export all revision IDs, `down_revision`, branch labels, and dependency
  edges to a review artifact.
- [ ] Record the live database value from `alembic_version` for every
  environment that may be upgraded.
- [ ] Compare the bodies and schema effects of:
  - `e2f3a4b5c6d7` and the alternate cycle-audit revision whose module declares
    `e3f4a5b6c7d8`.
  - `f0a1b2c3d4e6` and `f0a1b2c3d4f7` for online-learner controls/audits.
  - `f0a1b2c3d4e7` and `f3c4d5e6f7a8` for `log_finding.prompt_version`.
- [ ] Compare column types, nullability, indexes, constraints, defaults,
  downgrade behavior, and data transformations—not only revision IDs.

### 4.2 Select the canonical path

- [ ] Select the branch that matches the live production database parent and
  the actual model contract.
- [ ] If both branches are semantically identical and neither has been
  applied, replace them with one canonical revision before release.
- [ ] If either branch may already be applied, preserve its revision ID and
  create a merge/correction path that does not recreate existing tables or
  indexes.
- [ ] If the branches differ, write a data-preserving merge migration with
  explicit inspection of existing tables/columns before each operation.
- [ ] Add an operator note explaining why the selected revision is safe for
  each known database state.

### 4.3 Migration acceptance tests

- [ ] `alembic heads` returns exactly one head.
- [ ] `alembic history --verbose` has no unexplained branch or orphan.
- [ ] Upgrade an empty PostgreSQL database from base to head.
- [ ] Upgrade a database stamped at the current production revision to head.
- [ ] Verify tables, columns, indexes, unique constraints, and foreign keys.
- [ ] Run downgrade on a disposable database and verify the expected schema.
- [ ] Take a tested backup before the staging migration.
- [ ] Run a staging migration rehearsal with timing and rollback evidence.
- [ ] Do not run `alembic upgrade head` on production until all previous checks
  pass and an operator approves the migration window.

**Exit criteria:** one head, no duplicate table/index creation, clean empty-DB
upgrade, successful production-like upgrade, and documented rollback.

### 4.4 Historical evidence recorded on 2026-09-19

The revision IDs in the historical evidence below describe the intermediate
graph reviewed on 2026-09-19. The current canonical graph is the single
`m20260921rgwmetrics` head reported in Section 2; these older IDs are retained
for audit history and are not current blockers.

- [x] Live database revision confirmed as `f4e5f6a7b8c9`; no live migration was
  executed during this work.
- [x] Canonical production path confirmed as
  `e3f4a5b6c7d8 -> f0a1b2c3d4f7 -> ... -> f4e5f6a7b8c9`.
- [x] The four unapplied orphan revisions were removed from the working tree:
  `e1f2a3b4c5d6`, `e2f3a4b5c6d7`, `f0a1b2c3d4e6`, and `f0a1b2c3d4e7`.
  Their tables are absent from the live database.
- [x] Before the RR-03 worktree change appeared, `alembic heads` returned one
  head: `f5a6b7c8d9e0`.
- [x] Offline upgrade from `f4e5f6a7b8c9` to `f5a6b7c8d9e0` contains only the
  `volume_dependency_snapshots` table, its two indexes, its cluster foreign
  key, and the version update.
- [x] Empty disposable PostgreSQL database upgraded from `base` to head.
- [x] Production-like disposable PostgreSQL database upgraded from `f4` to
  head; the final schema and version were verified.
- [x] Disposable PostgreSQL database downgraded from head to `base` after
  making the historical forecast-drift downgrade ownership-safe. The
  `d8e9f0a1b2c3` upgrade path is unchanged; its downgrade no longer removes
  columns owned by `d4f7a1c9e2b6`.
- [x] The RR-03 migration `m20260919forecastevents` was subsequently reviewed
  and committed as the parent of `m20260919nlcontext`; the current graph now has
  one head at `m20260921tgoutbox`, and the migration round-trip evidence is
  recorded in the release manifest.
- [x] Freeze and review the current graph after the RR-03 migration was
  accepted; further production migration rehearsal remains a separate gate.
- [ ] Backup validation and staging migration rehearsal remain pending.
- [ ] Operator approval and production migration window remain pending.

### 4.5 Current graph and pending-head rehearsal evidence (2026-09-21)

- [x] `alembic heads` returns only `m20260921rgwmetrics`; `alembic history --verbose`
  shows the linear tail `m20260919forecastevents -> m20260919nlcontext ->
  m20260921tgoutbox -> m20260921rgwmetrics`, with no unexplained head.
- [x] The connected database was inspected read-only with `alembic current` and
  remains at `m20260921tgoutbox`; no production migration was executed.
- [x] Offline SQL from the current database revision to the code head creates only
  `rgw_metric_snapshots`, its bounded `(cluster_id, captured_at)` index, and the
  expected `alembic_version` update.
- [x] A disposable SQLite rehearsal stamped at `m20260921tgoutbox` upgraded to
  `m20260921rgwmetrics`, verified the table and index, then downgraded back and
  verified that the table was removed.
- [ ] PostgreSQL empty-database, production-like, backup, and staging rehearsal
  remain pending; do not run `alembic upgrade head` on the connected database
  until those gates and operator approval are complete.

## 5. Workstream RR-03 — restore the forecast-event model contract

### 5.1 Decide the canonical domain model

- [x] Compared the historical orphan forecast-event migration with the
  existing `NodeResourceForecastTransition` model and consumers. The orphan
  revision was not applied and was removed from the graph.
- [x] Selected `NodeResourceForecastAlertEvent` mapped to
  `node_resource_forecast_alert_events` as the new canonical canary event
  stream. The legacy transition table remains as a compatibility source while
  rollout is in progress; it is not treated as an alias.
- [x] Added the new ORM model and migration, runtime schema detection in the
  canary reader, and dual-write from the forecast watcher when the new table
  exists. This keeps code compatible with a database that has not migrated
  yet.

### 5.2 Tests

- [x] Restore import/collection for `tests/test_migrations.py`.
- [x] Verify the model columns exactly match the migration table.
- [x] Verify new-event reads, legacy fallback reads, ordering, and
  cluster/host/metric scope.
- [x] Run migration tests against SQLite and PostgreSQL disposable databases;
  upgrade and downgrade both pass for the new event migration.
- [x] Run the complete forecast test suite after the existing four
  node-resource alert regressions are repaired.

### 5.3 RR-03 verification result

- `5 passed` for the new event/fallback/migration-focused test set.
- Full selected run after the fix: `27 passed` including the complete
  `test_node_resource_forecast.py` module, canary tests, event fallback tests,
  and the forecast-event migration test.
- No production database was migrated.

### 5.4 Forecast regression root cause

- The runtime schema check used `inspect(engine)` after pending ORM writes.
  With SQLite `StaticPool`, the inspector reused the session connection and
  rolled back the pending alert insert. The transition/event rows could then
  remain without their parent alert because SQLite test foreign keys were not
  enforced.
- The check now uses the session connection and is evaluated once before
  writes. This preserves the transaction and works for pre- and post-RR-03
  schemas.

**Exit criteria:** no missing import, migration table and ORM model agree, and
all forecast event tests pass without compatibility hacks.

## 6. Workstream RR-04 — align online-learning controls

- [x] Define `is_paused(session, cluster_id, host, metric)` semantics:
  missing control means `RUNNING`; explicit `PAUSED` means no learning write
  or promotion for that exact scope.
- [x] Normalize cluster, host, and metric through the same scope function used
  by `get_control()` and `set_status()`.
- [x] Implement `is_paused()` with a fail-closed database error policy for
  production paths.
- [x] Ensure the consumer checks the control before state mutation, forecast
  promotion, and alert emission.
- [x] Add tests for missing control, paused scope, running scope, another
  cluster, and another metric.
- [x] Add audit evidence for pause, resume, reset, and rejected writes.
- [x] Add explicit malformed-scope and database-error regression tests.

### 6.1 RR-04 verification result

- Control and consumer tests: `9 passed`.
- A paused scope returns `runtime_mode="PAUSED"`, does not create an
  `OnlineLearnerAudit`, and does not write learner state.

**Exit criteria:** `tests/test_online_learning_controls.py` passes and the
consumer cannot write or promote data for a paused scope.

## 7. Workstream RR-05 — move Telegram delivery out of transactions

### 7.1 Immediate safe fix

- [x] Inside the database transaction, create/update Incident, Action, audit,
  and an alert-delivery record or pending event.
- [x] Commit the transaction before invoking Telegram/AI enrichment.
- [x] Deliver only after commit using the committed incident ID and immutable
  payload.
- [x] If commit fails, send no external alert.
- [x] If delivery fails after commit, retain the incident and protect the
  Watcher transaction; the current immediate fix logs the failure. Durable
  retry state remains part of the outbox follow-up below.

### 7.1.1 Immediate-fix evidence

- watcher/node_health_monitor.py now queues hardware alerts inside the
  transaction and calls Telegram only after session.commit() returns.
- OSD latency, CRUSH skew, database-size, default/observed-cluster Incident,
  verification, and recovery paths use the same outbox.
- Telegram exceptions cannot roll back Incident, Action, or audit rows; failed
  delivery is retried and can be replayed from DEAD by an admin.
- Previous core regression groups pass 271 tests; the specialized
  outbox/forecast/RCA/log/Vitastor/Trash group passes 131 tests; the final
  watcher/worker aggregate regression passes 456 tests.

### 7.2 Preferred transactional-outbox design

- [x] Add an outbox table with event ID, incident ID, category, payload hash,
  status, attempts, next retry time, created/sent timestamps, and last error.
- [x] Insert the outbox row in the same transaction as the durable producer event; default Incident, node-health, OSD latency, CRUSH skew, database-size, verification, recovery, all specialized watcher producers, and the backup worker legacy managed-channel/AI sender are migrated. Other periodic and worker alert paths also use the outbox.
- [x] Add a bounded worker that claims rows safely and sends notifications
  outside the transaction.
- [x] Make delivery idempotent by event ID/fingerprint.
- [x] Add retry backoff and dead-letter state, bounded worker, operator replay, and delivery metrics; replay is admin-only and resets only explicitly selected DEAD rows.
- [x] Redact tokens, secrets, key material, and untrusted command output.
- [x] Migrate the backup worker's independent global/cluster Telegram path to a
  backup_alert outbox payload; only severity/message/job/cluster metadata is
  persisted, while bot token and chat ID are resolved at delivery time.
- [x] Preserve backup AI enrichment, managed-channel delivery, per-cluster
  channel isolation, disabled-channel no-op behavior, retry/dead-letter
  handling, and add regression/secret-persistence coverage (27 passed).

### 7.3 Required tests

- [x] Incident persists when Telegram is unavailable.
- [x] No Telegram message is sent from the transaction when DB commit fails; delivery is claimed only after commit.
- [x] One incident produces one initial notification through the event-id unique constraint.
- [x] Retry does not duplicate a successful notification because SENT rows are not claimable.
- [x] Resolve/recovery notifications are separate and idempotent event types.
- [x] AI/Telegram delivery failure leaves the incident committed and retryable.
- [x] Node-health tests pass without relying on an open transaction.

**Exit criteria:** all four node-health failures are fixed and transaction
boundaries are visible in code review and tests.

## 8. Workstream RR-06 — make pytest collection reproducible

- [x] Add the repository test scope to `pyproject.toml`:

  ```toml
  [tool.pytest.ini_options]
  testpaths = ["tests"]
  addopts = "-m 'not live and not integration'"
  markers = ["live: ...", "integration: requires an external service"]
  ```

- [x] Keep `transfer/` tests out of the default release suite unless they are
  intentionally migrated into `tests/`.
- [x] Add a CI check that prints the collected test root and rejects collection
  outside tests/ via scripts/ci/verify_pytest_collection.py.
- [x] Add explicit commands for live/integration suites rather than relying on
  accidental discovery; the workflow now exposes manual integration and gated
  live jobs.
  - `.venv/bin/pytest --collect-only -q`
- [x] Mark RabbitMQ tests as `integration` and keep them available through an
  explicit `pytest -m integration` run instead of making a broker mandatory
  for the default deterministic gate.

### 8.1 RR-06 verification result

- `pyproject.toml` now sets `testpaths = ["tests"]` and excludes both
  `live` and external `integration` tests by default.
- Latest collection result after the integration marker: `3934/3950 tests collected
  (16 deselected)`.
- `transfer/test_dashboard_pgs.py` is no longer collected and no collection
  error was reported.
- CI now runs the collection-root verifier and exposes explicit integration/live jobs.
- [~] Full default suite was re-run after the outbox changes; it reached
  approximately 68% with no test failure, then stalled for more than 15 minutes
  in SQLite migration setup on ext4 journal fsync (jbd2_log_wait_commit,
  process state D). The test-only process was terminated; this is an I/O
  environment blocker, not a pytest assertion failure.
- [x] The complete collected default node set was then executed in four
  deterministic shards on tmpfs: `3934 passed`, zero failures/errors, and all
  four shard processes exited with code 0. The single-process run remains a
  performance/environment issue because it exceeds the practical timeout even
  though the sharded run is green.
- [x] Focused post-change regression groups pass 271 tests; Telegram dashboard,
  verification, recovery, and outbox route groups pass 89 tests.

- [ ] Re-run the default suite after the new integration marker and record a
  clean exit; run `pytest -m integration` separately with a dedicated broker.
- [x] Add CI enforcement for collection roots and an explicit live-suite job.

**Exit criteria:** default collection has no import error, no `transfer/`
tests, and the default suite excludes live tests deterministically.

## 9. Workstream RR-07 — remove repository artifacts

- [x] Classify every tracked and untracked `*.bak`, `*.bak-*`, schema dump,
  `.env.bak-*`, and generated evidence file.
- [x] Confirm no removed backup contains a unique migration, required rollback data, or
  an uncommitted production fix before removal.
- [x] Remove tracked backup files from the current Git tree going forward; do not rewrite
  published history without an explicit repository-owner decision.
- [x] Add ignore rules:

  ```gitignore
  *.bak
  *.bak-*
  backups/*.sql
  ```

- [ ] Keep controlled schema evidence in CI artifacts or an access-restricted
  backup store, not under `dashboard/static/` or application source paths.
- [x] Verify the removed backup paths are no longer present under the static
  asset tree; a static-server smoke test remains recommended.
- [x] Scan the final diff and image context for high-signal secrets, tokens, cookies,
  database URLs, key paths, and private schema data; no high-signal secret signature
  was found in the final source scan or image scan.

### 9.1 RR-07 verification result

- Removed 9 tracked backup/schema artifacts; untracked recovery copies remain
  outside Git and are now ignored.
- `git status` no longer lists the `.env.bak-*`, `*.bak-*`, or `backups/*.sql`
  recovery files.
- Published Git history was not rewritten.
- A high-signal scan of tracked application source found no token, API-key,
  or private-key signature. Seventy-two tracked .codex-stage/transfer review
  artifacts remain outside the Dockerfile COPY paths; deletion is deferred until
  the owner confirms they are not recovery evidence.
- The initial image build exposed ignored `*.bak-*` files under copied source
  directories; `.dockerignore` now excludes `*.bak`, `*.bak-*`, recursive backup/schema
  paths, `.codex-stage`, and `transfer`. A clean rebuild (`ceph-ai:rr07-scan`, image
  ID `aeae2e51fc40`) contains no backup/review artifacts and no high-signal secret
  signature.

**Exit criteria:** no backup/schema artifact is tracked or included in the
release image, and the final repository status is clean.

## 10. Workstream RR-08 — update UI and navigation contracts

- [x] Update `tests/test_dashboard_openstack.py` so it checks the current
  intended navigation contract rather than requiring the removed `/volumes`
  link.
- [x] Keep the supported user flow explicit: Block Storage Overview → click a
  volume → volume detail page.
- [x] Existing Block Storage regression coverage verifies that the Overview
  table links to
  `/volumes/{pool}/{image}`.
- [x] Add a regression test that volume detail marks Block Storage Overview as
  active without exposing a redundant Volumes sidebar item.
- [ ] Run a browser smoke test after CSS/JS shell changes.

### 10.1 RR-08 verification result

- `tests/test_dashboard_openstack.py` plus the volume-detail navigation regression:
  `22 passed`.
- The auth-user page asserts `/block-storage` as the supported navigation
  target and rejects the removed standalone `/volumes` link; volume detail keeps
  Block Storage Overview active without restoring a redundant `/volumes` item.

**Exit criteria:** OpenStack UI tests pass and the navigation tests reflect the
current product decision.

## 11. Secondary hardening after P0 blockers

- [x] Replace remaining `datetime.utcnow()` calls with the shared UTC helper
  backed by `datetime.now(timezone.utc)`, preserving the legacy naive-UTC database
  representation without changing stored semantics.
- [x] Verify RabbitMQ integration with a dedicated broker user and vhost.
- [ ] Browser-test CSRF HTML buffering with large upload/form responses and
  streaming/error responses.
- [ ] Verify `X-Forwarded-Proto` trust is restricted to configured proxies and
  that production cookies receive `Secure` behind TLS termination.
- [ ] Add HSTS only when HTTPS enforcement is verified for every production
  entry point.

Integration evidence (2026-09-21): `pytest -q -m integration` passed all 3
RabbitMQ tests using a temporary dedicated user/vhost; the user, vhost, and
test database were removed by the test cleanup trap. The default `guest` account
was not used because RabbitMQ correctly rejects it over the published interface.

UTC warning cleanup evidence (2026-09-21): all remaining six `datetime.utcnow()`
call sites were replaced with `shared.time.utc_now()`; the focused Vitastor, AI
observability, and rollout-report regression set passed `45` tests.

## 12. Release test matrix

The release candidate is blocked until all applicable rows are green:

| Gate | Command/evidence | Required result |
| --- | --- | --- |
| Syntax | `python -m compileall dashboard shared watcher worker` | pass |
| Diff hygiene | `git diff --check` | no errors |
| Migration graph | `.venv/bin/alembic heads` | exactly one head |
| Empty DB migration | disposable PostgreSQL upgrade | pass |
| Existing DB migration | staging backup + upgrade rehearsal | pass and rollback documented |
| Collection | `.venv/bin/pytest --collect-only -q` | no import error, only `tests/` |
| Unit/integration | `.venv/bin/pytest -q` | zero failures/errors |
| Node alerts | node-health test module | zero failures |
| Learning controls | online-learning control tests | zero failures |
| UI navigation | OpenStack/Block Storage tests + browser smoke | pass |
| Security | security regression suite and secret scan | pass |
| Runtime | health checks, logs, queue, DB, migrations | healthy |
| Rollback | documented image/commit/migration rollback drill | pass |

## 13. Release procedure

- [ ] Freeze feature work while P0 blockers are being resolved.
- [ ] Create a release branch/checkpoint and record the starting SHA.
- [ ] Implement and test one workstream at a time in the order RR-01 → RR-08.
- [ ] Review migration diff and transaction boundaries with a second engineer.
- [ ] Run the complete test matrix on a clean checkout, not only the dirty
  server worktree.
- [ ] Build the image from the clean checkout and scan the image contents.
- [ ] Deploy to staging with autonomous remediation disabled.
- [ ] Run migration, health, alert-delivery, UI smoke, and rollback drills.
- [ ] Record final SHA, one migration head, image digest, config/flag diff,
  test output, backup path, and operator approval.
- [ ] Promote to production only after all P0/P1 gates are green.

## 14. Definition of done

This plan can move from `Plan/in-progress` to `Plan/completed` only when:

1. `alembic heads` returns one head.
2. Full default pytest collection and execution pass.
3. Node-health incident persistence and notification delivery are decoupled.
4. Forecast event model and online-learning controls match their migrations.
5. Backup/schema artifacts are removed or moved outside the repository.
6. UI tests and browser smoke tests match the current navigation.
7. A clean release checkout builds and starts successfully.
8. Staging rollback has been rehearsed and documented.
9. Autonomous remediation remains disabled until a separate approval gate.

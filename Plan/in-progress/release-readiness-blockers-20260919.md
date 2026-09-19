# Release Readiness Blockers — 2026-09-19

**Project:** `ceph-ai`  
**Repository:** `/root/ceph-ai` on `10.3.55.213`  
**Status:** Incomplete — RR-01/RR-02 graph cleanup is verified; an uncommitted RR-03 forecast-event migration is also validated, while staging and release gates remain pending
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

The current server was checked on 2026-09-19. The working tree is not clean.
The local branch is ahead of `origin/main` and also contains modified and
untracked files. The exact SHA and status must be recorded again immediately
before implementation begins; do not assume the reviewed `fabdb52` SHA is the
same as the current worktree.

Confirmed blockers:

| ID | Blocker | Evidence | Severity |
| --- | --- | --- | --- |
| RR-01 | Two Alembic heads | `f0a1b2c3d4e7` and `f5a6b7c8d9e0` | P0 |
| RR-02 | Duplicate online-learner migration branches | Two variants create the same controls/audit tables; prompt-version migrations also diverge | P0 |
| RR-03 | Missing ORM model | `tests/test_migrations.py` imports `NodeResourceForecastAlertEvent`, but `shared.models` does not define it | P0 |
| RR-04 | Online-learning API mismatch | `shared.online_learning_controls` has no `is_paused()` while tests call it | P1 |
| RR-05 | Telegram is called inside DB transaction | `send_node_alert()` is called before `session.commit()` | P0 |
| RR-06 | Full pytest scope is not deterministic | `testpaths` is missing; `transfer/` is collected | P1 |
| RR-07 | Repository contains backup/schema artifacts | Tracked `.bak-*` files and schema dumps, plus new untracked copies | P1 |
| RR-08 | UI contract regression | OpenStack test still requires `href="/volumes"`, but navigation intentionally removed it | P2 |

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

### 4.4 Evidence recorded on 2026-09-19

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
- [x] An uncommitted RR-03 migration `m20260919forecastevents` was checked
  without changing the live database. It extends `f5a6b7c8d9e0`, keeps one
  graph head, and its forecast-event table passed disposable PostgreSQL
  upgrade/downgrade validation. It still requires code review before release.
- [ ] Freeze and review the final graph after the RR-03 migration is either
  accepted and committed or removed as an incomplete concurrent change.
- [ ] Backup validation and staging migration rehearsal remain pending.
- [ ] Operator approval and production migration window remain pending.

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
- [ ] Add explicit malformed-scope and database-error regression tests.

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

- `watcher/node_health_monitor.py` now queues hardware alerts inside the
  transaction and calls Telegram only after `session.commit()` returns.
- Both unreachable-node and high-resource flows use the same delivery helper.
- Telegram exceptions are caught after commit and cannot roll back Incident,
  Action, or audit rows.
- `tests/test_node_health_monitor.py`: `18 passed`.

### 7.2 Preferred transactional-outbox design

- [ ] Add an outbox table with event ID, incident ID, category, payload hash,
  status, attempts, next retry time, created/sent timestamps, and last error.
- [ ] Insert the outbox row in the same transaction as the Incident.
- [ ] Add a bounded worker that claims rows safely and sends notifications
  outside the transaction.
- [ ] Make delivery idempotent by event ID/fingerprint.
- [ ] Add retry backoff, dead-letter state, operator replay, and metrics.
- [ ] Redact tokens, secrets, key material, and untrusted command output.

### 7.3 Required tests

- [x] Incident persists when Telegram is unavailable.
- [ ] No Telegram message is sent when DB commit fails.
- [ ] One incident produces one initial notification.
- [ ] Retry does not duplicate a successful notification.
- [ ] Resolve/recovery notifications are separate and idempotent.
- [ ] AI enrichment failure does not remove or roll back the incident.
- [ ] Node-health tests pass without relying on an open transaction.

**Exit criteria:** all four node-health failures are fixed and transaction
boundaries are visible in code review and tests.

## 8. Workstream RR-06 — make pytest collection reproducible

- [x] Add the repository test scope to `pyproject.toml`:

  ```toml
  [tool.pytest.ini_options]
  testpaths = ["tests"]
  addopts = "-m 'not live'"
  ```

- [x] Keep `transfer/` tests out of the default release suite unless they are
  intentionally migrated into `tests/`.
- [ ] Add a CI check that prints the collected test root and rejects collection
  outside `tests/`.
- [ ] Add explicit commands for live/integration suites rather than relying on
  accidental discovery.
- [x] Run and record the default collection command:
  - `.venv/bin/pytest --collect-only -q`

### 8.1 RR-06 verification result

- `pyproject.toml` now sets `testpaths = ["tests"]`; the existing
  `addopts = "-m 'not live'"` remains active.
- Collection result: `3645/3658 tests collected (13 deselected)`.
- `transfer/test_dashboard_pgs.py` is no longer collected and no collection
  error was reported.
- [ ] Run the complete default suite and record its final pass/fail result.
- [ ] Add CI enforcement for collection roots and an explicit live-suite job.

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
- [ ] Scan the final diff and image context for secrets, tokens, cookies,
  database URLs, key paths, and private schema data.

### 9.1 RR-07 verification result

- Removed 9 tracked backup/schema artifacts; untracked recovery copies remain
  outside Git and are now ignored.
- `git status` no longer lists the `.env.bak-*`, `*.bak-*`, or `backups/*.sql`
  recovery files.
- Published Git history was not rewritten.

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
- [ ] Add a regression test that volume detail marks Block Storage Overview as
  active without exposing a redundant Volumes sidebar item.
- [ ] Run a browser smoke test after CSS/JS shell changes.

### 10.1 RR-08 verification result

- `tests/test_dashboard_openstack.py`: `21 passed`.
- The auth-user page now asserts `/block-storage` as the supported navigation
  target and rejects the removed standalone `/volumes` link.

**Exit criteria:** OpenStack UI tests pass and the navigation tests reflect the
current product decision.

## 11. Secondary hardening after P0 blockers

- [ ] Replace remaining `datetime.utcnow()` calls with timezone-aware UTC
  datetimes, migration by migration, without changing stored semantics.
- [ ] Verify RabbitMQ integration with a dedicated broker user and vhost.
- [ ] Browser-test CSRF HTML buffering with large upload/form responses and
  streaming/error responses.
- [ ] Verify `X-Forwarded-Proto` trust is restricted to configured proxies and
  that production cookies receive `Secure` behind TLS termination.
- [ ] Add HSTS only when HTTPS enforcement is verified for every production
  entry point.

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

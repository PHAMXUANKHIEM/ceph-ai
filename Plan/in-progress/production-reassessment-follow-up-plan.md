# Production reassessment follow-up plan

**Project:** `ceph-ai`  
**Target:** `10.3.55.213:/root/ceph-ai`  
**Baseline:** `58534acf82298908d4794b3d9350b02dfa8e0aa0`  
**Status:** Open — release candidate remains blocked  
**Rule:** Do not mark a task `[x]` without the command, result, artifact and
environment recorded below. Do not enable destructive automation or live DR as
part of this plan.

## 0. Verified baseline and release blockers

- [x] Confirm `HEAD == origin/main == 58534acf`.
- [x] Reproduce `tests/test_watcher_incident_flow.py` on the server:
  `36 passed, 1 failed, 1 warning`; failure is
  `test_multiple_simultaneous_checks_create_one_incident_each_and_publish_all`
  with `RuntimeError: incident <id> disappeared after commit` at
  `watcher/main.py:787`.
- [x] Confirm the failing test passes in isolation and in a two-test subset;
  some larger/combined subsets are intermittent. This is evidence of order or
  fixture/session leakage, not a valid deterministic pass.
- [x] Verify GitHub Actions run `35506516273`: Python 3.11 and 3.12 test jobs
  failed; `quality` and `deploy` did not run because their dependency chain was
  not green.
- [x] Confirm the current workflow makes quality report-only and lets `deploy`
  depend on `test` rather than `quality`.
- [ ] Do not update the release manifest to “full suite passed” until the
  GitHub matrix is green on the exact pushed SHA.

## 1. P0 — watcher deterministic incident lifecycle

### 1.1 Reproduce and isolate the state leak

- [ ] Run the failing module 20 consecutive times in one process and 20 fresh
  processes on Python 3.11 and 3.12.
- [ ] Run the failing test after every preceding test individually, then use
  binary-search subsets to identify the smallest order-dependent sequence.
- [ ] Record `pytest --setup-show`, `--durations=50`, JUnit XML, warning summary,
  Python/SQLAlchemy versions and SQLite URL/pool configuration.
- [ ] Add a temporary diagnostic only on the investigation branch: incident
  IDs before/after each commit, connection identity, transaction state and
  row-count. Remove diagnostic output before merge.

### 1.2 Fix the transaction boundary

- [ ] Review the code at `watcher/main.py` lines 777–791 and its observed
  cluster twin. The current comment says mute inheritance stays in one
  transaction, but the code commits, re-queries, then inherits mute and commits
  again. Make the reviewed contract explicit:
  `flush → capture id → inherit mute in the same transaction → one commit`.
- [ ] Never use `expire_on_commit=False` as a global workaround.
- [ ] Preserve the PostgreSQL unique-index race handling: duplicate active
  incident is skipped, unrelated `IntegrityError` is raised.
- [ ] Ensure the envelope is built only after the transaction has committed,
  using scalar/immutable values rather than a detached ORM object.
- [ ] Apply the same lifecycle fix to the observed-cluster creation path.

### 1.3 Make fixtures hermetic

- [ ] Give every watcher test a database lifecycle finalizer that closes all
  sessions and disposes the test engine after each test.
- [ ] Decide and document one test database model: per-test temporary SQLite
  file for lifecycle tests, or a deliberately shared in-memory connection with
  explicit connection ownership. Do not mix both implicitly.
- [ ] Reset mutable watcher/module state, event-bus publishers, cache, locks,
  monkeypatches and async publisher tasks between tests.
- [ ] Add a regression specifically ordering
  `test_capacity_incident_freezes_structured_metric_evidence` before the
  simultaneous-check test.

### 1.4 Acceptance gate

- [ ] Focused watcher file: zero failures in 20 repeats on Python 3.11 and
  3.12.
- [ ] Focused watcher/security group: zero failures in 10 repeats.
- [ ] Full default suite: two consecutive green runs per Python version,
  including JUnit artifacts and warning counts.
- [ ] GitHub Actions run for the pushed SHA: both matrix jobs green.
- [ ] Update PR-01.1/PR-01.3 and the release manifest only after all evidence
  is attached.

## 2. P0 — CI quality gates and release reporting

### 2.1 Required dependency chain

- [ ] Change `deploy.needs` to include both `test` and `quality`.
- [ ] Add an explicit release-gate job that consumes test and quality outputs;
  deploy remains blocked unless all required checks pass.
- [ ] Keep live/destructive tests manual and approval-only.
- [ ] Prevent a push to `main` from deploying a failed or unreviewed release;
  use a protected environment/approval for the lab deploy.

### 2.2 Baseline-aware but blocking quality checks

- [ ] Ruff: generate a committed baseline for existing findings, fail on new
  findings and burn down the baseline by owner/file. Current observed baseline
  is approximately 243 findings and must not be hidden with `|| true`.
- [ ] Mypy: commit scope/configuration and a baseline; fail on new errors in
  changed paths, then ratchet to full typed scope.
- [ ] Bandit: fail on HIGH and policy-defined MEDIUM findings; document any
  intentional subprocess/temp-file exceptions with rule-scoped suppressions.
- [ ] `pip-audit` and production `npm audit`: fail on known exploitable
  vulnerabilities unless an expiry-bound waiver is recorded.
- [ ] Add Trivy image scan, SBOM generation, image digest and dependency hashes
  to the release artifact.
- [ ] Add coverage and warning budget by category; a new application warning
  fails the gate unless waived with owner and expiry.

### 2.3 Release evidence

- [ ] Generate one machine-readable report containing SHA, Python/Node versions,
  dependency hashes, image digest, Alembic head, test counts, warnings, scan
  results, rollback SHA and artifact links.
- [ ] Reconcile the manifest with the actual GitHub run; never state full-suite
  pass from a local-only run.

## 3. P0 — environment, autonomy and runtime ownership

- [ ] Resolve the identity mismatch: database currently reports `CS-LAB` as
  `autonomy_environment=production` and `autopilot_enabled=true`, while the
  application settings resolve to `development`.
- [ ] Before any change, export a redacted configuration snapshot and audit
  evidence. Then obtain operator decision: lab/staging identity or production
  identity, one canonical environment value, and intended autopilot state.
- [ ] Keep autopilot and destructive remediation disabled until that decision is
  signed. Verify the effective value in DB, `.env`, container environment and
  runtime logs after restart.
- [ ] Choose exactly one runtime owner (Podman or systemd), record owner,
  version, health checks and rollback command; disable the other owner only
  after its inventory and recovery path are stored.
- [ ] Move code-repair out of the production runtime/repository write path, or
  obtain a documented root-exception approval with a separate workspace and
  immutable artifact promotion.
- [ ] Remove `curl | sh`; download a versioned artifact, verify checksum/signature
  and install from a controlled path.
- [ ] Enforce deny-by-default egress and verify allowed destinations for Ceph,
  PostgreSQL, RabbitMQ and AI provider.

## 4. P1 — browser and feature acceptance

- [ ] Provide a non-production test credential or dedicated staging identity;
  never use the production password hash as a test credential.
- [ ] Run authenticated Chromium/Playwright smoke at 1/5/10 tabs and capture
  p50/p95/p99, reconnect, stale badge, duplicate requests and Ceph/SSH query
  counts. Store trace/video/HAR only on failure and redact cookies/tokens.
- [ ] Cinder: run against a disposable OpenStack controller for project,
  attachment, orphan, pagination, tenant isolation and eventual consistency;
  retain cluster-scoped audit evidence.
- [ ] RBD mirroring: use a disposable peer only; test status/lag/RPO,
  checksum/recovery point, planned failover/failback, fencing, split-brain,
  rollback and operator confirmation. No production peer mutation.
- [ ] Backup: run two-cluster parallel jobs, per-cluster digest/restore policy,
  inactive-cluster rejection, audit scope and secret redaction.
- [ ] RGW: validate Prometheus metric names, labels, cardinality and retention;
  run typed remediation preview/approval/post-check/rollback plus alert dedupe,
  silence, retry and no-action modes.
- [ ] AI post-check/rollback: audit every action contract for bounded timeout,
  fresh telemetry, health floor, before/after evidence, lease recovery and
  tested inverse action; inject post-check, rollback and partial-success
  failures.

## 5. P1 — staging, rollback, PostgreSQL and DR

- [ ] Assign explicit staging/canary/production IDs, owners, maintenance
  window, feature flags, allowed actions and data-isolation boundary.
- [ ] On staging PostgreSQL: backup → migration upgrade → health/API/browser
  smoke → application rollback → migration rollback/re-upgrade → backup restore;
  verify checksum, audit rows, RPO and RTO.
- [ ] Kill Worker, Watcher and Dashboard at pre-defined phases; prove resume,
  lease reconciliation and no duplicate action.
- [ ] Roll back using immutable app SHA and image digest, never `latest`.
- [ ] Complete one maintenance-cycle soak with p95/p99, warnings/errors,
  memory/disk, queue depth, duplicate-owner and alert-flood report.
- [ ] Only after written safety approval, run live DR on an isolated target with
  backup chain/RBD mirror, fencing, failover/failback, measured RPO/RTO and
  cleanup. A unit or scratch test cannot be called live DR pass.

## 6. Sign-off and completion rules

- [ ] Security owner signs cookie/proxy, container, SSH/mount, egress, image,
  dependency and secret-scan evidence.
- [ ] Operations owner signs environment identity, runtime owner, backup,
  rollback, kill switch and maintenance window.
- [ ] Release manifest records observed SHA, rollback SHA, image digest,
  migration backup/rehearsal, canary, residual risks and approval expiry.
- [ ] Only then mark the corresponding subitems `[x]`.
- [ ] Move the plan to `Plan/completed/` only when all mandatory P0/P1 gates are
  green, live-DR status is explicitly accepted or formally deferred with an
  owner/expiry, and no unapproved production automation remains.

## 7. Current ownership/blocker table

| Workstream | Current state | Required owner/evidence |
|---|---|---|
| Watcher deterministic suite | P0 failing/intermittent | Engineering + CI matrix |
| Quality gates | Report-only | Engineering + repository branch protection |
| Environment/autopilot | Mismatch; unsafe to infer | Operator/Operations |
| Runtime ownership | Podman active, systemd inactive | Operations |
| Browser auth | Credential unavailable | Operator/Staging |
| Cinder/RBD/backup/RGW | Fixture evidence only | Staging owners |
| PostgreSQL rollback | SQLite rehearsal only | DBA/Operations |
| Live DR | Not run | DR owner + written safety approval |
| Final production sign-off | Pending | Engineering, Security, Operations |

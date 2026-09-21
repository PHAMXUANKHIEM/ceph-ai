# Ceph AI end-to-end release manifest

Release ID: `ceph-ai-production-readiness-2026-09-21`

This manifest records the current release-candidate evidence for the
end-to-end completion plan. It is intentionally reviewable without exposing
credentials or provider content.

## Target

- Host: `10.3.55.213`
- Repository: `/root/ceph-ai`
- Branch: `main`
- Observed application commit: `d0866d219cf26a877b57b0181973c59f08ddcc53`
- Rollback candidate: `PENDING_OPERATOR_SELECTION`
- Worktree: not clean at evidence collection time; unrelated concurrent
  feature changes and the production-readiness workflow/scripts were present.
  This manifest must not be treated as an approved deployment.
- Deployment: not approved; running Podman services are externally managed

## Migration and artifacts

- Alembic head: `m20260921rgwmetrics` (one head)
- `alembic current`: `m20260921rgwmetrics (head)` on the target database
- Application image: `localhost/ceph-ai:local`
- Observed local image ID: `sha256:21c5b037228c25bb7a57b49a648d0bd0cfd7f925dc8be6be909582b9b72050e2`
- Frontend build: passed with Node 20 at
  `/opt/ceph-ai-node20/bin/node` (`npm run build`)
- Full deterministic Python suite: `3934 passed, 16 deselected, 248
  warnings` in `1869.59s`; RC=0. This is local-host evidence, not yet a
  GitHub Actions matrix result for this SHA.
- Cross-cutting hardening gate: `110 passed, 1 warning` in `21.72s`; RC=0
- Snapshot-page realtime gate: `138 passed, 1 warning` in `37.78s`; Node 20
  syntax checks passed for the shared bridge, PGs, Nodes, and CRUSH scripts.
- Disposable SQLite migration round-trip: `upgrade=0 downgrade=0
  reupgrade=0`; PostgreSQL staging rehearsal remains pending.
- Disposable SQLite backup rehearsal: backup artifact mode `0600`, migration
  head restored to `m20260921rgwmetrics`, 105 tables present after re-upgrade.

## Safety controls

- Global and per-cluster autonomous settings remain subject to operator review;
  no production autopilot promotion is performed by this plan.
- Secret values are not recorded. The repository `.env` is root-only (`0600`).
- Backup directory review is pending: `/var/backups/ceph-ai` exists with
  root-only permissions; `/var/lib/ceph-ai/backups` is absent.
- Runtime preflight found healthy Podman Dashboard/Worker/Watcher containers
  under a systemd-launched Podman stack and no competing legacy service unit;
  operator ownership sign-off is still pending.
- Database currently contains one `CS-LAB` cluster
  (`ac23b8ff-e235-414c-bed8-06894f3dedd3`) marked
  `autonomy_environment=production` with `autopilot_enabled=true`; this is an
  unresolved operator safety/sign-off blocker and was not changed by this
  plan. Application environment currently resolves as `development`, another
  identity mismatch requiring operator review.
- Production preflight failed closed on three unresolved gates: explicit
  `CEPH_AI_ENVIRONMENT` is unset, `AUTOPILOT_ENABLED=true`, and the host still
  has `OUTPUT ACCEPT` with no declared `CEPH_AI_EGRESS_POLICY=deny-by-default`.
  No live configuration was changed.
- Browser canary is incomplete: Chromium local unauthenticated smoke reached
  `/login` for 1/5/10 tabs; no valid operator credential was available for an
  authenticated run.

## Verification log

- `tests/test_ceph_debug.py`: `4 passed`
- `python -m py_compile dashboard/routes/system_health.py`: passed
- Node 20 production frontend build: passed
- `alembic heads`: one head
- `scripts/deploy/production_readiness_preflight.sh`: failed closed with the
  three identity/autonomy/egress blockers above; CPU/memory/swap budget passed.
- `scripts/deploy/staging_migration_rehearsal.sh`: safety-gated rehearsal
  runner added; not executed because no isolated staging PostgreSQL target was
  provisioned.
- `scripts/ci/release_gate.py`: added machine-readable CI release evidence for
  JUnit, quality, pip-audit, image scan, SBOM, migration head and immutable
  image ID. GitHub Actions execution is still required before this gate can be
  marked passed for a release SHA.
- Disposable migration: upgrade head → downgrade base → upgrade head passed
- Disposable migration backup: `scripts/deploy/backup_database_before_migration.sh`
  created a root-only `0600` SQLite artifact; checksum/size were recorded in
  the rehearsal log without storing secrets.
- `pip-audit -l`: no known vulnerabilities. Bandit after the fingerprint fix:
  0 HIGH, 23 MEDIUM, 37 LOW; Ruff baseline remains 243 findings and is not
  yet a blocking CI gate.
- DR/backup/RGW/AI acceptance batch: `175 passed, 1 warning`.
- Full suite command: `.venv/bin/pytest -q -k 'not live and not integration'`
- Full suite result: passed; no unexplained failures
- Hardening command: `.venv/bin/pytest -q tests/test_api_rate_limit.py
  tests/test_audit.py tests/test_dashboard_audit.py tests/test_dashboard_auth.py
  tests/test_dashboard_health_api.py tests/test_dashboard_navigation.py
  tests/test_logging_redaction.py tests/test_nl_security.py tests/test_redaction.py
  tests/test_remediation_runbook.py tests/test_security_audit.py
  tests/test_service_health.py`

## Approval and rollback

- Operator approval: `PENDING`
- Approved deploy commit: `PENDING` (code is pushed but deployment is not authorized)
- Migration backup/rehearsal: `PARTIAL` (disposable SQLite passed; PostgreSQL
  backup/restore, staging witness and operator approval remain pending)
- Image scan/SBOM: CI workflow added, execution for the observed SHA pending
- Browser authenticated smoke: `PENDING` (no non-production credential)
- Live isolated-target DR drill: `PENDING` (target, safety approval and
  operator witness not provisioned)
- Rollback procedure: `Plan/in-progress/end-to-end-feature-completion-and-deployment-plan.md`
- Automatic destructive remediation: disabled pending explicit per-cluster approval

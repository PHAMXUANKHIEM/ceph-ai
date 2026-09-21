# Ceph AI end-to-end release manifest

Release ID: `ceph-ai-e2e-2026-09-19`

This manifest records the current release-candidate evidence for the
end-to-end completion plan. It is intentionally reviewable without exposing
credentials or provider content.

## Target

- Host: `10.3.55.213`
- Repository: `/root/ceph-ai`
- Branch: `main`
- Observed application commit: `3394789a` (`security: mark log fingerprints
  non-cryptographic`)
- Rollback candidate: `6b4b122d`
- Worktree: one pre-existing unrelated change remains in
  `Plan/in-progress/natural-language-ceph-ai-plan.md`; this release work is
  otherwise committed. `origin/main` is 12 commits behind the observed SHA and
  deployment/push approval is not inferred from this manifest.
- Deployment: not approved; running Podman services are externally managed

## Migration and artifacts

- Alembic head: `m20260919nlcontext`
- `alembic current`: `m20260919nlcontext (head)`
- Application image: `localhost/ceph-ai:local`
- Frontend build: passed with Node 20 at
  `/opt/ceph-ai-node20/bin/node` (`npm run build`)
- Full deterministic Python suite: `3817 passed, 47 deselected, 235
  warnings` in `1742.85s`; RC=0
- Cross-cutting hardening gate: `110 passed, 1 warning` in `21.72s`; RC=0
- Snapshot-page realtime gate: `138 passed, 1 warning` in `37.78s`; Node 20
  syntax checks passed for the shared bridge, PGs, Nodes, and CRUSH scripts.
- Disposable SQLite migration round-trip: `upgrade=0 downgrade=0
  reupgrade=0`
- Disposable SQLite backup rehearsal: backup artifact mode `0600`, migration
  head restored to `m20260919nlcontext`, 105 tables present after re-upgrade.

## Safety controls

- Global and per-cluster autonomous settings remain subject to operator review;
  no production autopilot promotion is performed by this plan.
- Secret values are not recorded. The repository `.env` is root-only (`0600`).
- Backup directory review is pending: `/var/backups/ceph-ai` exists with
  root-only permissions; `/var/lib/ceph-ai/backups` is absent.
- Runtime ownership conflict remains: healthy Podman Dashboard/Worker/Watcher
  containers run independently of inactive systemd units.
- Database currently contains one `CS-LAB` cluster
  (`ac23b8ff-e235-414c-bed8-06894f3dedd3`) marked
  `autonomy_environment=production` with `autopilot_enabled=true`; this is an
  unresolved operator safety/sign-off blocker and was not changed by this
  plan. Application environment currently resolves as `development`, another
  identity mismatch requiring operator review.
- Browser canary is incomplete: Chromium local unauthenticated smoke reached
  `/login` for 1/5/10 tabs; no valid operator credential was available for an
  authenticated run.

## Verification log

- `tests/test_ceph_debug.py`: `4 passed`
- `python -m py_compile dashboard/routes/system_health.py`: passed
- Node 20 production frontend build: passed
- `alembic heads`: one head
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
  backup/restore and operator witness remain pending)
- Rollback procedure: `Plan/in-progress/end-to-end-feature-completion-and-deployment-plan.md`
- Automatic destructive remediation: disabled pending explicit per-cluster approval

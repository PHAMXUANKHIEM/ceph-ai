# Ceph AI end-to-end release manifest

Release ID: `ceph-ai-e2e-2026-09-19`

This manifest records the current release-candidate evidence for the
end-to-end completion plan. It is intentionally reviewable without exposing
credentials or provider content.

## Target

- Host: `10.3.55.213`
- Repository: `/root/ceph-ai`
- Branch: `main`
- Observed application commit: `55a510cd6b205e3ea3e1a6606dad6571197eb266`
- Rollback candidate: `origin/main` at the same commit
- Worktree: dirty; review and commit separation are still required
- Deployment: not approved; running Podman services are externally managed

## Migration and artifacts

- Alembic head: `m20260919nlcontext`
- `alembic current`: `m20260919nlcontext (head)`
- Application image: `localhost/ceph-ai:local`
- Frontend build: passed with Node 20 at
  `/opt/ceph-ai-node20/bin/node` (`npm run build`)
- Full deterministic Python suite: `3817 passed, 47 deselected, 235
  warnings` in `1742.85s`; RC=0
- Disposable SQLite migration round-trip: `upgrade=0 downgrade=0
  reupgrade=0`

## Safety controls

- Global and per-cluster autonomous settings remain subject to operator review;
  no production autopilot promotion is performed by this plan.
- Secret values are not recorded. The repository `.env` is root-only (`0600`).
- Backup directory review is pending: `/var/backups/ceph-ai` exists with
  root-only permissions; `/var/lib/ceph-ai/backups` is absent.
- Runtime ownership conflict remains: healthy Podman Dashboard/Worker/Watcher
  containers run independently of inactive systemd units.
- Canary scope is the explicitly identified `CS-LAB` cluster
  (`ac23b8ff-e235-414c-bed8-06894f3dedd3`); production scope is not assigned.

## Verification log

- `tests/test_ceph_debug.py`: `4 passed`
- `python -m py_compile dashboard/routes/system_health.py`: passed
- Node 20 production frontend build: passed
- `alembic heads`: one head
- Disposable migration: upgrade head → downgrade base → upgrade head passed
- Full suite command: `.venv/bin/pytest -q -k 'not live and not integration'`
- Full suite result: passed; no unexplained failures

## Approval and rollback

- Operator approval: `PENDING`
- Approved deploy commit: `PENDING`
- Migration backup/rehearsal: `PENDING`
- Rollback procedure: `Plan/in-progress/end-to-end-feature-completion-and-deployment-plan.md`
- Automatic destructive remediation: disabled pending explicit per-cluster approval

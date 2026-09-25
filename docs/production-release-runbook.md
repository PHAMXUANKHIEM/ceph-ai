# Ceph AI production release runbook

This runbook is the operator-facing companion to the strict production
readiness plan. It applies to a release candidate built from one immutable
source SHA and one image digest.

## Release identity

Before a release, generate the manifest and documentation report:

```bash
python scripts/ci/documentation_freshness.py \
  --output artifacts/release/documentation-freshness.json
python scripts/ci/release_manifest.py \
  --environment staging \
  --output artifacts/release/release-manifest.json
```

The release is not eligible when the manifest reports a dirty worktree,
multiple Alembic heads, a missing image digest, expired documentation, or
missing required evidence.

## Required sequence

1. Run deploy preflight and save its redacted JSON/log output.
2. Verify the source SHA, image digest, SBOM, dependency lock hash and
   migration head.
3. Create and verify a PostgreSQL backup before migration.
4. Run the staging migration rehearsal against PostgreSQL, not SQLite.
5. Apply the migration only after the rehearsal and backup checks pass.
6. Restart services idempotently and verify Dashboard, Watcher, Worker,
   RabbitMQ consumer and migration head.
7. Run authenticated read-only smoke tests on the target host.
8. Record phase logs, running image digests, health evidence and operator
   witness in the release artifact.

## Rollback

Container rollback and database rollback are separate operations. A failed
health check may roll back the immutable application image only when the
database schema remains compatible. A migration rollback requires a reviewed
downgrade or forward-fix plan and an isolated PostgreSQL rehearsal.

Never reset the development worktree to perform a deployment rollback.
Rollback must select the previous immutable release checkout/image digest.

## AI safety gate

River v2 and other candidate models remain shadow-only until independent
verified outcomes, paired evaluation, operator approval, audit evidence and a
rollback rehearsal are present. The release process must not enable
autonomous remediation merely because unit tests or a benchmark fixture pass.

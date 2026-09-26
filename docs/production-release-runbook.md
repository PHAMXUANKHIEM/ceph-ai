# Ceph AI production release runbook

This runbook is the operator-facing companion to the strict production
readiness plan. It applies to a release candidate built from one immutable
source SHA and one image digest.

> **Availability:** single node, no HA. See the availability model in
> [`operations/reliability-slo.md`](operations/reliability-slo.md) before
> promising recovery times to anyone.

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

For an isolated PostgreSQL restore rehearsal, use an explicit source and a
different staging target. The command rejects production target IDs and
identical source/target endpoints:

```bash
python scripts/deploy/postgresql_restore_rehearsal.py \
  --source-url "$STAGING_SOURCE_DATABASE_URL" \
  --source-id staging-source-20260925 \
  --target-url "$STAGING_RESTORE_DATABASE_URL" \
  --target-id staging-restore-20260925 \
  --confirm I_UNDERSTAND_STAGING_ONLY \
  --backup-dir /var/lib/ceph-ai/rehearsal-backups \
  --report artifacts/postgresql-restore-rehearsal.json
```

The report is evidence for review, not a production approval. Run failure
injection with `--inject-failure before_restore`, `after_restore`,
`before_migration` or `after_migration` only against the disposable target.

## Post-deploy smoke

`scripts/deploy/post_deploy_smoke.py` runs on the target host at the end of
every deploy and writes `deploy-evidence/post-deploy-smoke.json`. It fails the
deploy when the login page, the Watcher/Worker heartbeats, the database
migration head or the release SHA of any service image is wrong.

The authenticated dashboard check is `SKIPPED` until a dedicated low-privilege
smoke account exists. Create one in the Dashboard, then store it on the target
host (never in Git or in CI secrets that reach GitHub-hosted runners):

```text
install -m 0600 /dev/null /var/lib/ceph-ai/config/smoke-credentials
printf 'smoke-user:<password>\n' > /var/lib/ceph-ai/config/smoke-credentials
```

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

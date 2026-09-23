# Ceph-AI production deployment

This is the production operating model. Do not use the legacy SQLite/nohup
quickstart as a production procedure.

## Required components

- PostgreSQL with encrypted transport and a tested backup/restore path.
- RabbitMQ with durable `incidents` and dead-letter queues.
- A pinned application image built once in CI and deployed by digest.
- Dedicated read-only SSH credentials for Watcher/Dashboard and a separate
  mutation identity for the Worker/executor.
- TLS termination, trusted-host/origin configuration, non-default dashboard
  credentials and a random session secret of at least 32 bytes.

## Release sequence

For a production rollout, deploy the exact image digest produced and scanned by
CI. The deploy script refuses a pre-built image without an expected digest and
checks the digest of every application container after restart:

```bash
export CEPH_AI_IMAGE="registry.example/ceph-ai@sha256:<scanned-image-digest>"
export CEPH_AI_EXPECTED_IMAGE_DIGEST="sha256:<local-image-id>"
DEPLOY_REF="<full-commit-sha>" bash scripts/deploy/restart_container_stack.sh
```

Do not set `CEPH_AI_ALLOW_UNVERIFIED_IMAGE=true` in production. That override
exists only for an explicitly non-production lab test and is intentionally not
part of the normal release workflow.

The local development path without `CEPH_AI_IMAGE` still builds the image on
the host. It is not an immutable production deployment and must not be used as
production evidence.

1. Verify the source SHA, dependency evidence, image scan, SBOM and image digest.
2. Run the release gate on a clean runner; missing or malformed evidence fails.
3. Create a PostgreSQL backup and record its checksum and restore metadata.
4. Rehearse the migration on an isolated PostgreSQL target.
5. Apply migrations only after the image and rollback plan are ready.
6. Deploy the exact scanned image digest; do not rebuild on the host.
7. Run authenticated Dashboard, Worker queue and health checks.
8. Keep the system in `ADVISORY` or `APPROVAL_REQUIRED` until operator sign-off.

The canonical commands are:

```bash
.venv/bin/python scripts/ci/release_gate.py --require-ci-artifacts
bash scripts/deploy/run_migrations.sh
DEPLOY_REF=<full-commit-sha> bash scripts/deploy/restart_container_stack.sh
```

The deployment script must refuse a dirty source checkout except for explicitly
allowlisted documentation paths. Record the release evidence artifact together
with source SHA, migration head, image digest, backup path, rollback SHA and
operator approval.

## Rollback and incident response

- Stop autonomous remediation before rollback.
- Preserve the release evidence, application logs and outbox metrics.
- Roll back the application image and configuration first.
- Use a forward-compatible recovery migration unless a tested downgrade exists.
- Restore PostgreSQL only on an approved recovery target and record the witness.
- Validate RabbitMQ consumption, stale snapshots, audit entries and Telegram
  delivery after recovery.

Production is not considered released until PostgreSQL rehearsal, browser smoke,
RabbitMQ/DB failure tests, DR evidence and security/operations sign-off are all
available for the exact SHA.

# Immutable production release (operator runbook)

The normal Compose stack uses one registry image for Dashboard, Telegram,
Watcher, Worker, Full Executor and Vault Monitor. It does not mount source code.
Code Repair is a separate host service and may edit an isolated worktree, but
its auto-push, staging deployment and main promotion are disabled until a
candidate-specific scan and registry pipeline exists. Single Full's permissions
are unchanged.

## CI release identity

On a push to `main`, CI builds the Dockerfile with the Git SHA label, scans the
image, creates an SBOM, then pushes that same image to GHCR. The quality
artifact contains `registry-image-ref.txt` in the form
`ghcr.io/owner/repo@sha256:<64 hex digits>`. Never deploy the mutable SHA tag,
`latest`, or `ceph-ai:local` in production.

The Dockerfile pins its Python base by manifest digest and installs
`requirements-prod.lock` with `--require-hashes`. Regenerate the lock for
Python 3.11 with `pip-compile pyproject.toml --generate-hashes
--output-file=requirements-prod.lock --resolver=backtracking` in a clean
tool environment. Review and commit the lock and Dockerfile together.

## Deployment

CI stops at the release gate: there is no automatic deploy job. The former
self-hosted `ceph-ai-lab` runner was not the Ceph AI host, so the deploy and
live jobs were removed on 2026-09-26. An operator deploys on the Ceph AI host
itself, taking the immutable reference from the `quality-security-evidence`
artifact (`registry-image-ref.txt`) of a green CI run:

```text
cd /root/ceph-ai
CEPH_AI_IMAGE=ghcr.io/<owner>/ceph-ai@sha256:<digest> DEPLOY_REF=<sha> \
  CEPH_AI_DEPLOY_EVENT_LOG=/var/lib/ceph-ai/release-artifacts/deploy-phases.log \
  bash scripts/deploy/deploy_preflight.sh && \
  bash scripts/deploy/restart_container_stack.sh
```

`restart_container_stack.sh`
refuses a dirty checkout, checks out the exact SHA, pulls the approved image,
compares the image's revision label, takes a database backup, runs migrations
from the image, then restarts the stack. It checks container health, queue
consumption, each running image ID and the Dashboard login endpoint.

`/var/lib/ceph-ai/release-artifacts/current-image-ref` is the boot-time source
of truth. `previous-image-ref` holds the last approved digest. Production
startup refuses to fall back to `ceph-ai:local` if no approved reference exists.
Do not edit either file manually.

## Rollback

Rehearse PostgreSQL compatibility on staging before changing the application
version. A container rollback does *not* reverse the database migration. With
the previous digest still available and schema compatibility confirmed, run:

```sh
CEPH_AI_ROLLBACK_ACK_COMPATIBLE_SCHEMA=yes \
  bash scripts/deploy/rollback_container_stack.sh
```

The script pulls the previous registry digest, restarts containers and verifies
health and image IDs. On failure it restores the current reference and tries
to restart the current image. If the migration is incompatible, use the
documented database backup/restore procedure in a controlled maintenance
window instead of running this container-only rollback.

## Still required before production sign-off

- Run the entire GitHub Actions workflow on a clean runner and confirm GHCR
  permissions, pushed digest and pulled digest match.
- Rehearse backup, migration, failure after migration, and rollback against a
  PostgreSQL staging copy; record timings and operator approval.
- Pin the apt repository snapshot and transitive OS packages, or record the
  remaining non-reproducibility as an accepted release risk.
- Verify a fresh host's account mounts, CLI packages, SSH identity, secrets,
  network access, log rotation and disk headroom before the first rollout.

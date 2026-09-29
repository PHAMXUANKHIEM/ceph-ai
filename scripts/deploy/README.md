# CI/CD deploy setup

## Isolated release checkout (implementation in progress)

`release_checkout.sh` prepares an independent checkout under
`/var/lib/ceph-ai/releases/<full-sha>` without resetting or cleaning the
development checkout. It accepts only a full commit SHA, verifies the detached
HEAD and clean worktree, and supports atomically switching `/var/lib/ceph-ai/current`
or restoring the previous checkout:

```bash
scripts/deploy/release_checkout.sh prepare <full-40-character-sha>
scripts/deploy/release_checkout.sh activate <full-40-character-sha>
scripts/deploy/release_checkout.sh rollback
```

This helper is not wired into the live systemd/Compose owner yet. Do not use
`activate` on the production host until the container unit and all host-side
maintenance units have been migrated to `/var/lib/ceph-ai/current` and the
rollback/smoke sequence has been rehearsed. The existing deployment entrypoint
still operates on its configured repository checkout.

## Rollout preflight and failure handling

`restart_container_stack.sh` now runs `deploy_preflight.sh` before its checkout,
migration, or restart phases. If restart, health, consumer, or smoke validation
fails, the script records the failed phase. Automatic container rollback is
disabled by default; it runs only when the operator explicitly sets
`CEPH_AI_ROLLBACK_ACK_COMPATIBLE_SCHEMA=yes`, confirming that the deployed
database schema is compatible with the previous image. This never rolls back
PostgreSQL schema. The rollback script validates container health, the
Dashboard health/login endpoints, and the RabbitMQ incidents consumer before
recording success.

`.github/workflows/ci-cd.yml` runs the test suite on every push/PR to
`main`, then (push to `main` only, after tests pass) SSHes into this server
and runs `restart_services.sh` to pull the latest code and restart
watcher/worker/dashboard.

> `git pull` alone is not a deployment. Python/Uvicorn keeps the FastAPI
> route table imported at process startup, so a newly-added page such as
> `/pgs` remains 404 until Dashboard is restarted. On every server/checkout,
> deploy code changes with:
>
> ```bash
> bash scripts/deploy/restart_services.sh
> ```
>
> The script now verifies `/pgs` after restart and fails loudly if an old
> Dashboard process is still serving port 8000.

## One-time setup on this server

```bash
ssh-keygen -t ed25519 -f ~/.ssh/ceph_aiops_deploy_key -N "" -C "github-actions-deploy-ceph-ai"
cat ~/.ssh/ceph_aiops_deploy_key.pub >> ~/.ssh/authorized_keys
cat ~/.ssh/ceph_aiops_deploy_key   # copy this into the DEPLOY_SSH_KEY secret below, then treat it as sensitive
```

Optional — pin this server's real dashboard bind address (never commit a
real IP into the repo):

```bash
echo 'DASHBOARD_HOST=<real-ip-or-0.0.0.0>' > scripts/deploy/deploy.local.env
```

## Repo secrets to add on GitHub (Settings → Secrets and variables → Actions)

| Secret | Value |
|---|---|
| `DEPLOY_HOST` | This server's IP/hostname, reachable from GitHub's runners |
| `DEPLOY_USER` | `root` (matches this deployment's existing operational model — every service already runs as root, no systemd/deploy-user isolation exists yet) |
| `DEPLOY_SSH_KEY` | The **private** key generated above (`~/.ssh/ceph_aiops_deploy_key`) |
| `DEPLOY_PORT` | Optional, defaults to `22` |
| `DEPLOY_PATH` | Absolute path to this checkout on the server, e.g. `/root/source-code-vita/ceph-aiops` |

## Network note

GitHub's hosted runners connect from GitHub's own cloud IP ranges, not from
your network. If this server's firewall/SSH only accepts connections from
specific known IPs, the deploy step will fail to connect — either allow
GitHub's runner IP ranges, or switch to a self-hosted Actions runner
installed directly on this server (avoids exposing SSH to the internet
and avoids storing a deploy key in GitHub Secrets at all, at the cost of
maintaining a runner process here).

# CI/CD deploy setup

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

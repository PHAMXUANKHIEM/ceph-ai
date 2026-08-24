# AI code repair supervisor

`worker.code_repair` repairs failures in the Ceph AIOps application itself;
it does not execute Ceph remediation actions. It extracts the newest error
from application logs, redacts credentials, creates an isolated Git worktree,
asks Codex or Claude Code for a minimal patch and regression test, then applies
path and test gates before committing and optionally pushing a dedicated
branch.

Example on staging:

```bash
cd /root/ceph-ai
PYTHONPATH=. .venv/bin/python -m worker.code_repair \
  --log /var/log/ceph-ai-watcher.log \
  --log /var/log/ceph-ai-worker.log \
  --log /var/log/ceph-ai-dashboard.log \
  --provider auto --push
```

For this dedicated staging host, the complete autonomous path is explicit:

```bash
PYTHONPATH=. .venv/bin/python -m worker.code_repair \
  --provider auto --push --deploy-staging --promote-main
```

`--promote-main` is accepted only after the candidate has passed the staging
deployment controller. A protected remote branch may still reject promotion,
which is treated as a failed run without weakening repository protection.

Use `--evidence-file incident.txt` for a manually curated traceback. Repeated
errors are deduplicated in `/var/lib/ceph-ai/code-repair-state.json`; use
`--force` only after reviewing the previous failed attempt.

The generated branch is never merged or deployed automatically. A staging
deployment controller may deploy that branch, run smoke tests, and merge it
only after those tests pass. This separation ensures a restarted application
cannot terminate or falsely mark its own repair successful.

On the dedicated staging server, deploy and smoke-test a pushed candidate with:

```bash
bash scripts/deploy/ai_repair_candidate.sh ai-repair/<candidate-branch>
```

The controller reruns the complete test suite, deploys the detached candidate,
checks Dashboard/Watcher/Worker and requires a fresh successful Watcher
heartbeat. Any failure redeploys the exact previous commit automatically.

# AI code repair supervisor

`worker.code_repair` repairs failures in the Ceph AIOps application itself;
it does not execute Ceph remediation actions. It extracts the newest error
from application logs, redacts credentials, creates isolated Git worktrees,
asks a planning/review agent to investigate, asks an implementation agent for
a minimal patch and regression test, then lets the two agents exchange review
feedback for a bounded number of rounds. Path and test gates still run before
committing and optionally pushing a dedicated branch.

The default is two independent agents using the same selected provider. This
means two Codex processes are supported. Different providers and model ids can
be selected when both CLIs are installed/authenticated:

An administrator can save the same values in **Settings → Pipeline & lưu trữ →
AI Code Repair**. The page labels **Planner / Reviewer** as the role that asks,
analyses, and audits, and **Implementer** as the role allowed to edit the
isolated worktree and write tests. Saving the form updates `.env`; it does not
start a repair run, merge a branch, or deploy anything.

```bash
PYTHONPATH=. .venv/bin/python -m worker.code_repair \
  --provider auto \
  --planner-provider claude --planner-model sonnet \
  --implementer-provider codex --implementer-model gpt-5-codex \
  --max-review-rounds 2 --push
```

`--planner-provider` is used for both the initial plan and independent review;
`--implementer-provider` writes and revises the patch. Use `--max-review-rounds
0` only for diagnostics; normal repairs should retain the review gate. The
value is hard-limited to 5. Evidence from logs and `--evidence-file` is
redacted before it reaches either agent or Telegram. Candidate validation
also expands untracked files, rejects credential paths/symlinks, scans new
file contents, and checks both staged and unstaged changes. The model ids are
passed to the installed CLI, so availability depends on that CLI
account/catalog.

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

The staging gate excludes migration tests that require a newer SQLite than
the host OS supplies and RabbitMQ topology tests that require exclusive queue
ownership. Those remain mandatory in isolated CI; the staging gate runs the
remaining application suite plus the candidate's focused regression tests.

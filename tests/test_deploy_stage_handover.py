"""restart_container_stack.sh hands over to the deployed revision after checkout.

Deploys used to finish with the script of the checkout that existed before
the deploy, so units added in the new revision were not installed (twice on
2026-10-06/07), and `git reset` rewrote the running script under bash.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy" / "restart_container_stack.sh"

STAGE2 = """#!/usr/bin/env bash
set -euo pipefail
echo "STAGE2 stage=${CEPH_AI_DEPLOY_STAGE:-} head=$(git rev-parse HEAD) expected=${CEPH_AI_DEPLOY_EXPECTED_HEAD:-}"
"""


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
def test_the_deployed_revision_runs_everything_after_checkout(tmp_path):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    repo = tmp_path / "repo"
    _git(tmp_path, "clone", "-q", str(origin), str(repo))
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    deploy = repo / "scripts" / "deploy"
    deploy.mkdir(parents=True)
    shutil.copy(SCRIPT, deploy / "restart_container_stack.sh")
    preflight_log = tmp_path / "preflight.log"
    (deploy / "deploy_preflight.sh").write_text(f"#!/usr/bin/env bash\necho ran >> {preflight_log}\n")
    for script in deploy.iterdir():
        script.chmod(0o755)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "A: current deploy script")
    _git(repo, "branch", "-M", "main")
    _git(repo, "push", "-q", "origin", "main")
    (deploy / "restart_container_stack.sh").write_text(STAGE2)
    _git(repo, "commit", "-q", "-am", "B: new revision")
    target = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "reset", "-q", "--hard", "HEAD~1")  # the host still runs revision A

    result = subprocess.run(
        ["bash", str(deploy / "restart_container_stack.sh")], cwd=repo, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "DEPLOY_REF": target}, timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert f"STAGE2 stage=after-checkout head={target} expected={target}" in result.stdout
    assert preflight_log.read_text().count("ran") == 1  # preflight is not repeated


def test_stage_two_refuses_a_moved_checkout():
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'exec "$REPO_DIR/scripts/deploy/restart_container_stack.sh"' in text
    assert "run_until_checkout() {" in text and 'git reset --hard "$DEPLOY_REF"' in text.split("run_until_checkout() {")[1].split("\n}\n")[0]
    assert 'checkout moved between deploy stages' in text

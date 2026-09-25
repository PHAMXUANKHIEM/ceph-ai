"""Hermetic checks for the read-only deployment preflight."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/deploy/deploy_preflight.sh"
WORKFLOW = ROOT / ".github/workflows/ci-cd.yml"


def _executable(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)


def _environment(tmp_path: Path) -> dict[str, str]:
    deploy = tmp_path / "deploy"
    (deploy / ".git").mkdir(parents=True)
    alembic = deploy / ".venv/bin/alembic"
    alembic.parent.mkdir(parents=True)
    _executable(alembic, "exit 0\n")
    runtime = tmp_path / "runtime"
    backup = tmp_path / "backups"
    runtime.mkdir()
    backup.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _executable(
        fake_bin / "podman",
        'if [ "${1:-}" = compose ]; then exit 0; fi\n'
        'if [ "${1:-}" = login ]; then exit 0; fi\n'
        'if [ "${FAKE_RABBIT_FAIL:-0}" = 1 ]; then exit 1; fi\n'
        "exit 0\n",
    )
    _executable(fake_bin / "systemctl", "exit 0\n")
    env = os.environ.copy()
    env.update({
        "PATH": f"{fake_bin}:{env['PATH']}",
        "CEPH_AI_DEPLOY_DIR": str(deploy),
        "CEPH_AI_DATABASE_BACKUP_DIR": str(backup),
        "CEPH_AI_PREFLIGHT_REQUIRED_DIRS": str(runtime),
        "CEPH_AI_PREFLIGHT_REQUIRED_COMMANDS": "bash",
        "CEPH_AI_PREFLIGHT_SKIP_HOST_RUNTIME": "1",
        "CEPH_AI_PREFLIGHT_SKIP_NETWORK": "1",
        "DATABASE_URL": "postgresql+psycopg://operator:secret@db/ceph_ai",
        "GH_TOKEN": "test-token",
    })
    return env


def _run(tmp_path: Path, **updates: str) -> subprocess.CompletedProcess[str]:
    env = _environment(tmp_path)
    env.update(updates)
    return subprocess.run(
        ["bash", str(SCRIPT)], cwd=ROOT, env=env,
        text=True, capture_output=True, check=False,
    )


def test_preflight_passes_without_leaking_database_credentials(tmp_path):
    result = _run(tmp_path)
    output = result.stdout + result.stderr
    assert result.returncode == 0
    assert "status=PASS check=summary" in output
    assert "operator" not in output
    assert "secret" not in output


def test_preflight_names_a_missing_command(tmp_path):
    result = _run(tmp_path, CEPH_AI_PREFLIGHT_REQUIRED_COMMANDS="missing-ceph-ai-command")
    assert result.returncode == 1
    assert "status=FAIL check=command.missing-ceph-ai-command detail=missing" in result.stderr


def test_preflight_fails_before_deploy_on_missing_permission_target(tmp_path):
    missing = tmp_path / "does-not-exist"
    result = _run(tmp_path, CEPH_AI_PREFLIGHT_REQUIRED_DIRS=str(missing))
    assert result.returncode == 1
    assert f"check=permission.{missing} detail=missing-or-not-rwx" in result.stderr


def test_preflight_reports_registry_unavailable(tmp_path):
    env = _environment(tmp_path)
    _executable(Path(env["PATH"].split(":", 1)[0]) / "curl", "exit 22\n")
    env["CEPH_AI_PREFLIGHT_SKIP_NETWORK"] = "0"
    result = subprocess.run(["bash", str(SCRIPT)], cwd=ROOT, env=env,
                            text=True, capture_output=True, check=False)
    assert result.returncode == 1
    assert "check=registry-network detail=unreachable:" in result.stderr


def test_preflight_reports_rabbitmq_unavailable(tmp_path):
    result = _run(
        tmp_path,
        CEPH_AI_PREFLIGHT_SKIP_HOST_RUNTIME="0",
        FAKE_RABBIT_FAIL="1",
    )
    assert result.returncode == 1
    assert "check=rabbitmq detail=container-or-rabbitmqctl-unavailable" in result.stderr


def test_preflight_writes_a_bounded_report_on_failure(tmp_path):
    report = tmp_path / "evidence/deploy-preflight.log"
    report.parent.mkdir()
    result = _run(
        tmp_path,
        CEPH_AI_PREFLIGHT_REPORT=str(report),
        CEPH_AI_PREFLIGHT_REQUIRED_COMMANDS="missing-ceph-ai-command",
    )
    assert result.returncode == 1
    assert report.exists()
    assert report.stat().st_mode & 0o777 == 0o640
    assert "check=summary detail=1-checks-failed" in report.read_text()


def test_deploy_workflow_runs_preflight_before_registry_login_and_rollout():
    workflow = WORKFLOW.read_text()
    deploy_job = workflow[workflow.index("  deploy:"):workflow.index("  release_gate:")]
    preflight = deploy_job.index("bash scripts/deploy/deploy_preflight.sh")
    registry_login = deploy_job.index("podman login ghcr.io")
    rollout = deploy_job.index("bash scripts/deploy/restart_container_stack.sh")
    assert preflight < registry_login < rollout
    assert "if: always()" in deploy_job
    assert "deploy-preflight-evidence-${{ github.sha }}" in deploy_job

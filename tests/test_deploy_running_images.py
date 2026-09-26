"""The deploy records which image every service is actually running (plan 3.4)."""

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/deploy/restart_container_stack.sh"


def _consumer_phase() -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("start_phase consumer")
    end = text.index("finish_phase", start) + len("finish_phase")
    return text[start:end]


def _run(tmp_path, *, worker_image):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    podman = bin_dir / "podman"
    podman.write_text(
        "#!/usr/bin/env bash\n"
        'container="$2"; format="$4"\n'
        'if [ "$format" = "{{.ImageName}}" ]; then echo "ghcr.io/org/ceph-ai@sha256:abc"; exit 0; fi\n'
        f'if [ "$container" = "ceph-ai_worker_1" ]; then echo "{worker_image}"; else echo "approved-id"; fi\n',
        encoding="utf-8",
    )
    podman.chmod(0o755)
    records = tmp_path / "running-images.jsonl"
    script = (
        "set -euo pipefail\n"
        "start_phase() { :; }\nfinish_phase() { :; }\n"
        "SERVICES=(dashboard-web worker)\n"
        "approved_image_id=approved-id\nDEPLOY_IMAGE=ghcr.io/org/ceph-ai@sha256:abc\nconsumer_count=1\n"
        f"DEPLOY_RUNNING_IMAGES={records}\n"
        + _consumer_phase() + "\n"
    )
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    result = subprocess.run(["bash", "-c", script], env=env, text=True, capture_output=True, check=False)
    lines = [json.loads(line) for line in records.read_text(encoding="utf-8").splitlines()] if records.exists() else []
    return result, lines


def test_every_service_image_is_recorded_when_they_match(tmp_path):
    result, lines = _run(tmp_path, worker_image="approved-id")
    assert result.returncode == 0, result.stderr
    assert [line["service"] for line in lines] == ["dashboard-web", "worker"]
    assert all(line["matches"] is True for line in lines)
    assert lines[1]["container"] == "ceph-ai_worker_1"
    assert lines[1]["approved_ref"] == "ghcr.io/org/ceph-ai@sha256:abc"


def test_a_mismatched_service_is_recorded_before_the_deploy_fails(tmp_path):
    result, lines = _run(tmp_path, worker_image="stale-id")
    assert result.returncode == 5
    assert "not using the approved registry artifact" in result.stderr
    assert lines[-1]["service"] == "worker"
    assert lines[-1]["running_image_id"] == "stale-id"
    assert lines[-1]["matches"] is False

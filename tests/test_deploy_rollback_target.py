"""Deploys keep a valid rollback target and record it (plan 3.6)."""

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/deploy/restart_container_stack.sh"
OLD = "ghcr.io/org/ceph-ai@sha256:" + "a" * 64
NEW = "ghcr.io/org/ceph-ai@sha256:" + "b" * 64


def _persistence_block() -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index('install -d -m 0750 "$(dirname "$IMAGE_REF_FILE")"')
    end = text.index("# end of release reference persistence")
    return text[start:end]


def _deploy(tmp_path, image):
    refs = tmp_path / "release-artifacts"
    record = tmp_path / "rollback-target.json"
    script = (
        "set -euo pipefail\n"
        f"IMAGE_REF_FILE={refs}/current-image-ref\n"
        f"PREVIOUS_IMAGE_REF_FILE={refs}/previous-image-ref\n"
        f"DEPLOY_IMAGE={image}\nDEPLOY_ROLLBACK_RECORD={record}\n"
        + _persistence_block()
    )
    subprocess.run(["bash", "-c", script], check=True, capture_output=True, text=True)
    previous = refs / "previous-image-ref"
    return (
        (refs / "current-image-ref").read_text().strip(),
        previous.read_text().strip() if previous.exists() else None,
        json.loads(record.read_text()),
    )


def test_first_deploy_has_no_valid_rollback_target(tmp_path):
    current, previous, record = _deploy(tmp_path, OLD)
    assert (current, previous) == (OLD, None)
    assert record["rollback_ref_valid"] is False
    assert record["rollback_ref"] == ""


def test_new_release_rotates_the_previous_digest_into_the_rollback_target(tmp_path):
    _deploy(tmp_path, OLD)
    current, previous, record = _deploy(tmp_path, NEW)
    assert (current, previous) == (NEW, OLD)
    assert record == {
        "deploy_ref": NEW, "rollback_ref": OLD, "rollback_ref_valid": True,
        "rollback_scope": "container-only; database schema is not rolled back",
    }


def test_retrying_the_same_artifact_keeps_the_real_rollback_target(tmp_path):
    _deploy(tmp_path, OLD)
    _deploy(tmp_path, NEW)
    current, previous, record = _deploy(tmp_path, NEW)
    # Before the fix the retry copied NEW over previous-image-ref, leaving no
    # way back to OLD.
    assert (current, previous) == (NEW, OLD)
    assert record["rollback_ref"] == OLD
    assert record["rollback_ref_valid"] is True

"""CI rehearsal of the customer installer (plan R8.7): scripts/customer/ci_*.py|sh and the ci-cd.yml job."""

import re
import subprocess
from pathlib import Path

import yaml

from scripts.customer import ci_released_images

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci-cd.yml"
REHEARSAL = ROOT / "scripts" / "customer" / "ci_rehearsal.sh"


def test_the_two_newest_released_commits_are_picked_skipping_unreleased_ones():
    digests = {"c3": "sha256:" + "3" * 64, "c1": "sha256:" + "1" * 64, "c0": "sha256:" + "0" * 64}
    images = ci_released_images.released_images(["c4", "c3", "c2", "c1", "c0"], digests.get)
    assert images == ["ghcr.io/phamxuankhiem/ceph-ai@sha256:" + "3" * 64,
                      "ghcr.io/phamxuankhiem/ceph-ai@sha256:" + "1" * 64]


def test_fewer_than_two_released_images_is_reported(monkeypatch, capsys):
    monkeypatch.setattr(ci_released_images, "_token", lambda: "t")
    monkeypatch.setattr(ci_released_images, "first_parent_commits", lambda base: ["a", "b"])
    monkeypatch.setattr(ci_released_images, "digest_of", lambda tag, token: "sha256:" + "9" * 64 if tag == "a" else None)
    assert ci_released_images.main(["x", "main"]) == 1
    assert "found 1" in capsys.readouterr().err


def _job():
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]["customer-install"]


def test_the_job_rehearses_every_supported_os_with_pinned_images():
    job = _job()
    oses = job["strategy"]["matrix"]["os"]
    assert [o["name"] for o in oses] == ["ubuntu-22.04-podman-3.4", "ubuntu-24.04", "rocky-9"]
    for os_entry in oses:
        assert re.search(r"@sha256:[0-9a-f]{64}$", os_entry["image"]), os_entry
    assert job["strategy"]["fail-fast"] is False


def test_the_job_runs_only_for_installer_changes_and_then_rehearses():
    steps = _job()["steps"]
    gate = next(step for step in steps if step.get("id") == "changes")
    for path in ("deploy/customer/", "scripts/customer/", "ci-cd"):
        assert path in gate["run"]
    rehearse = next(step for step in steps if "ci_rehearsal.sh" in step.get("run", ""))
    assert rehearse["if"] == "steps.changes.outputs.run == 'true'"
    # In ci-cd.yml, so the release notifier's CI state includes it.
    assert WORKFLOW.name == "ci-cd.yml"


def test_the_rehearsal_script_is_valid_and_refuses_identical_images():
    subprocess.run(["bash", "-n", str(REHEARSAL)], check=True)
    image = "ghcr.io/x/y@sha256:" + "1" * 64
    result = subprocess.run(["bash", str(REHEARSAL), "img", image, image], capture_output=True, text=True)
    assert result.returncode == 2 and "must differ" in result.stderr

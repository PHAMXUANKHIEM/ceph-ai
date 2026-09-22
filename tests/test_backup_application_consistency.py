import hashlib
import json
from pathlib import Path

import pytest

from worker.backup import application_consistency as consistency


def _install_manifest(monkeypatch, tmp_path):
    pre = tmp_path / "freeze-hook"
    post = tmp_path / "thaw-hook"
    pre.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    post.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    pre.chmod(0o700)
    post.chmod(0o700)
    manifest = tmp_path / "backup-hooks.json"
    manifest.write_text(json.dumps({
        "hooks": {
            "qemu_guest_agent": {
                "pre": {
                    "command": [str(pre), "freeze", "{pool}", "{image}"],
                    "sha256": hashlib.sha256(pre.read_bytes()).hexdigest(),
                },
                "post": {
                    "command": [str(post), "thaw", "{pool}", "{image}"],
                    "sha256": hashlib.sha256(post.read_bytes()).hexdigest(),
                },
            }
        }
    }), encoding="utf-8")
    manifest.chmod(0o600)
    monkeypatch.setattr(consistency, "HOOK_MANIFEST_PATH", str(manifest))
    # The production check requires root-owned files. The test fixture is
    # intentionally temporary; exercise the command/checksum behavior here.
    monkeypatch.setattr(consistency, "_trusted_file", lambda path, executable: True)
    return pre, post, manifest


def test_application_policy_requires_allowlisted_freeze_and_thaw_hooks(monkeypatch, tmp_path):
    _install_manifest(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="cần cả pre_hook_id và post_hook_id"):
        consistency.normalize_policy({"consistency_mode": "application-consistent"})


def test_hooks_run_with_context_and_thaw_on_pre_failure(monkeypatch, tmp_path):
    pre, post, _manifest = _install_manifest(monkeypatch, tmp_path)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs["env"]["CEPH_AI_BACKUP_HOOK_PHASE"]))
        return type("Completed", (), {"stdout": "ok", "stderr": "", "returncode": 0})()

    monkeypatch.setattr(consistency.subprocess, "run", fake_run)
    policy = consistency.normalize_policy({
        "consistency_mode": "application-consistent",
        "application_consistency": {
            "pre_hook_id": "qemu_guest_agent",
            "post_hook_id": "qemu_guest_agent",
            "timeout_seconds": 9,
        },
    })
    session = consistency.begin(policy, "vms", "web-01")
    consistency.thaw(session, "vms", "web-01")
    consistency.thaw(session, "vms", "web-01")

    assert calls == [
        ([str(pre), "freeze", "vms", "web-01"], "pre-freeze"),
        ([str(post), "thaw", "vms", "web-01"], "post-thaw"),
    ]


def test_pre_hook_failure_still_attempts_thaw(monkeypatch, tmp_path):
    _install_manifest(monkeypatch, tmp_path)
    phases = []

    def fake_run(command, **kwargs):
        phase = kwargs["env"]["CEPH_AI_BACKUP_HOOK_PHASE"]
        phases.append(phase)
        if phase == "pre-freeze":
            return type("Completed", (), {"stdout": "", "stderr": "failed", "returncode": 2})()
        return type("Completed", (), {"stdout": "", "stderr": "", "returncode": 0})()

    monkeypatch.setattr(consistency.subprocess, "run", fake_run)
    policy = consistency.normalize_policy({
        "consistency_mode": "application-consistent",
        "application_consistency": {
            "pre_hook_id": "qemu_guest_agent",
            "post_hook_id": "qemu_guest_agent",
        },
    })
    with pytest.raises(consistency.ApplicationConsistencyError):
        consistency.begin(policy, "vms", "web-01")
    assert phases == ["pre-freeze", "post-thaw"]


def test_raw_command_array_and_unknown_hook_are_rejected(monkeypatch, tmp_path):
    _install_manifest(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="chỉ nhận pre_hook_id"):
        consistency.normalize_policy({
            "consistency_mode": "application-consistent",
            "application_consistency": {
                "pre_hook": ["/bin/sh", "-c", "id"],
                "post_hook": ["thaw"],
            },
        })
    with pytest.raises(ValueError, match="hook_id thuộc allowlist"):
        consistency.normalize_policy({
            "consistency_mode": "application-consistent",
            "application_consistency": {
                "pre_hook_id": "arbitrary_command",
                "post_hook_id": "qemu_guest_agent",
            },
        })


def test_manifest_shell_interpreter_is_rejected(monkeypatch, tmp_path):
    _pre, _post, manifest = _install_manifest(monkeypatch, tmp_path)
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["hooks"]["qemu_guest_agent"]["pre"] = {
        "command": ["/bin/sh", "-c", "id"],
        "sha256": hashlib.sha256(Path("/bin/sh").read_bytes()).hexdigest(),
    }
    manifest.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="không được dùng shell interpreter"):
        consistency.normalize_policy({
            "consistency_mode": "application-consistent",
            "application_consistency": {
                "pre_hook_id": "qemu_guest_agent",
                "post_hook_id": "qemu_guest_agent",
            },
        })
def test_hook_checksum_mismatch_fails_closed(monkeypatch, tmp_path):
    pre, _post, _manifest = _install_manifest(monkeypatch, tmp_path)
    pre.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum không khớp"):
        consistency.normalize_policy({
            "consistency_mode": "application-consistent",
            "application_consistency": {
                "pre_hook_id": "qemu_guest_agent",
                "post_hook_id": "qemu_guest_agent",
            },
        })

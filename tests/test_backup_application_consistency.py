import pytest

from worker.backup import application_consistency as consistency


def test_application_policy_requires_freeze_and_thaw_hooks():
    with pytest.raises(ValueError, match="cần cả pre_hook và post_hook"):
        consistency.normalize_policy({"consistency_mode": "application-consistent"})


def test_hooks_run_with_context_and_thaw_on_pre_failure(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs["env"]["CEPH_AI_BACKUP_HOOK_PHASE"]))
        return type("Completed", (), {"stdout": "ok", "stderr": "", "returncode": 0})()

    monkeypatch.setattr(consistency.subprocess, "run", fake_run)
    policy = consistency.normalize_policy({
        "consistency_mode": "application-consistent",
        "application_consistency": {
            "pre_hook": ["/usr/local/bin/freeze", "web-01"],
            "post_hook": ["/usr/local/bin/thaw", "web-01"],
            "timeout_seconds": 9,
        },
    })
    session = consistency.begin(policy, "vms", "web-01")
    consistency.thaw(session, "vms", "web-01")
    consistency.thaw(session, "vms", "web-01")

    assert calls == [
        (["/usr/local/bin/freeze", "web-01"], "pre-freeze"),
        (["/usr/local/bin/thaw", "web-01"], "post-thaw"),
    ]


def test_pre_hook_failure_still_attempts_thaw(monkeypatch):
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
            "pre_hook": ["freeze"],
            "post_hook": ["thaw"],
        },
    })
    with pytest.raises(consistency.ApplicationConsistencyError):
        consistency.begin(policy, "vms", "web-01")
    assert phases == ["pre-freeze", "post-thaw"]

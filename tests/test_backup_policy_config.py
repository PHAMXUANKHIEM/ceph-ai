import yaml
import pytest

from worker.backup import policy_config


def _policy():
    return {
        "backup_targets": [{"slot": "a", "immutable": False}, {"slot": "b", "immutable": True}],
        "required_copy_count": 2,
        "tracked_images": [{"pool": "rbd", "image": "web-01", "full_refresh_every_n_days": 30}],
        "rpo_hours": 24,
        "metadata_rpo_hours": 12,
        "restore_drill_rpo_hours": 192,
        "retention": {"keep_full_count": 3, "keep_incremental_count": 7},
    }


def test_save_policy_is_atomic_and_preserves_revision(monkeypatch, tmp_path):
    policy_path = tmp_path / "backup_policy.yaml"
    revision_dir = tmp_path / "revisions"
    policy_path.write_text(yaml.safe_dump(_policy()), encoding="utf-8")
    monkeypatch.setattr(policy_config, "POLICY_PATH", str(policy_path))
    monkeypatch.setattr(policy_config, "POLICY_REVISION_DIR", str(revision_dir))

    updated = _policy()
    updated["rpo_hours"] = 48
    result = policy_config.save_backup_policy(updated, actor="admin")

    assert policy_config.load_backup_policy()["rpo_hours"] == 48
    assert result["revision_id"]
    revisions = policy_config.list_policy_revisions()
    assert revisions[0]["revision_id"] == result["revision_id"]
    assert revisions[0]["actor"] == "admin"


@pytest.mark.parametrize("bad", [
    {"backup_targets": [{"slot": "a"}, {"slot": "a"}]},
    {"backup_targets": [{"slot": "a"}], "tracked_images": [{"pool": "bad/pool", "image": "x"}]},
    {"backup_targets": [{"slot": "a"}], "access_key": "secret"},
])
def test_policy_validation_rejects_unsafe_or_invalid_values(bad):
    with pytest.raises(policy_config.BackupPolicyValidationError):
        policy_config.validate_backup_policy(bad)

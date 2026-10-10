import logging

import pytest
from pydantic import ValidationError

from config.settings import RETIRED_SETTINGS, Settings


def _env(tmp_path, *lines):
    path = tmp_path / ".env"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_a_removed_setting_left_in_env_is_ignored_with_a_warning(tmp_path, caplog):
    """10/10/2026: CEPH_PATCH_NODE_STAGING_DIR outlived PR #43 and stopped the deploy and the nightly jobs."""
    env = _env(tmp_path, "CEPH_PATCH_NODE_STAGING_DIR=/opt/ceph-aiops-patch-staging", "DATABASE_POOL_SIZE=7")

    with caplog.at_level(logging.WARNING, logger="config.settings"):
        loaded = Settings(_env_file=env)

    assert loaded.database_pool_size == 7
    assert "CEPH_PATCH_NODE_STAGING_DIR" in caplog.text and "delete them" in caplog.text


def test_an_unknown_key_still_fails_so_typos_are_caught(tmp_path):
    env = _env(tmp_path, "CEPH_PATCH_NODE_STAGING_DIR=/x", "DATABSE_URL=sqlite:///typo.db")

    with pytest.raises(ValidationError, match="databse_url"):
        Settings(_env_file=env)


def test_retired_names_are_not_live_fields():
    assert not RETIRED_SETTINGS & set(Settings.model_fields)

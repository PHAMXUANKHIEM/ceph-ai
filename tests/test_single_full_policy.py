import pytest

from config.settings import settings
from shared.single_full_policy import (
    CodeRepairFullAccessDisabledError,
    ensure_code_repair_full_access_allowed,
)


def test_isolated_code_repair_requires_existing_automation_flag(monkeypatch):
    monkeypatch.setattr(settings, "ceph_ai_environment", "staging")
    monkeypatch.setattr(settings, "code_repair_auto_enabled", False)

    with pytest.raises(CodeRepairFullAccessDisabledError):
        ensure_code_repair_full_access_allowed()


def test_isolated_code_repair_is_denied_in_production(monkeypatch):
    monkeypatch.setattr(settings, "ceph_ai_environment", "production")
    monkeypatch.setattr(settings, "code_repair_auto_enabled", True)

    with pytest.raises(CodeRepairFullAccessDisabledError, match="production"):
        ensure_code_repair_full_access_allowed()

import pytest

from dashboard.dual_ai_chat import DualAIChatError, DUAL_WORKSPACE_ENV, _execution_repo


def test_dual_write_mode_uses_configured_isolated_workspace(monkeypatch, tmp_path):
    workspace = tmp_path / "dual-workspace"
    workspace.mkdir()
    (workspace / ".git").write_text("gitdir: isolated\n")
    monkeypatch.setenv(DUAL_WORKSPACE_ENV, str(workspace))

    assert _execution_repo(allow_writes=True, full_access=False) == workspace


def test_dual_write_mode_refuses_live_source_without_workspace(monkeypatch):
    monkeypatch.delenv(DUAL_WORKSPACE_ENV, raising=False)

    with pytest.raises(DualAIChatError, match="từ chối sửa source thật"):
        _execution_repo(allow_writes=True, full_access=False)

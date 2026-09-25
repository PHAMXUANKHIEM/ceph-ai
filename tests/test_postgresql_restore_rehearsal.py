import importlib.util
from argparse import Namespace
from pathlib import Path

import pytest


MODULE = Path(__file__).resolve().parents[1] / "scripts" / "deploy" / "postgresql_restore_rehearsal.py"
spec = importlib.util.spec_from_file_location("postgresql_restore_rehearsal", MODULE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def _args(tmp_path, **overrides):
    values = {
        "source_url": "postgresql://source:secret@staging-db/source",
        "source_id": "staging-source",
        "target_url": "postgresql://target:secret@restore-db/restore",
        "target_id": "staging-restore",
        "confirm": module.CONFIRMATION,
        "backup_dir": tmp_path / "backup",
        "report": tmp_path / "report.json",
        "inject_failure": "none",
        "timeout": 30,
        "pg_dump_bin": "/bin/true",
        "pg_restore_bin": "/bin/true",
        "psql_bin": "/bin/true",
    }
    values.update(overrides)
    return Namespace(**values)


def test_rehearsal_rejects_production_target(tmp_path):
    with pytest.raises(module.RehearsalError, match="target cannot be production"):
        module.run_rehearsal(_args(tmp_path, target_id="production"))


def test_rehearsal_rejects_same_source_and_target(tmp_path):
    with pytest.raises(module.RehearsalError, match="different from the source"):
        module.run_rehearsal(_args(
            tmp_path,
            target_url="postgresql://target:secret@staging-db/source",
        ))


def test_rehearsal_requires_explicit_confirmation(tmp_path):
    with pytest.raises(module.RehearsalError, match="confirmation token"):
        module.run_rehearsal(_args(tmp_path, confirm="NO"))


def test_rehearsal_injected_failure_is_reported_without_claiming_pass(tmp_path, monkeypatch):
    args = _args(tmp_path, inject_failure="before_restore")
    commands = []

    monkeypatch.setattr(module, "_command", lambda name, override=None: "/bin/true")
    monkeypatch.setattr(module, "_run", lambda command, **kwargs: commands.append(command) or "")
    # The fake pg_dump does not create a file, so provide the artifact after
    # command execution while keeping the production script's control flow.
    def fake_run(command, **kwargs):
        commands.append(command)
        if command[0] == "/bin/true" and "--format=custom" in command:
            output = Path(command[command.index("--file") + 1])
            output.write_bytes(b"backup")
        return ""

    monkeypatch.setattr(module, "_run", fake_run)
    result = module.run_rehearsal(args)
    assert result["status"] == "INJECTED_FAILURE"
    assert result["errors"] == ["injected failure before restore"]
    assert args.report.is_file()
    assert not any("--dbname" in command for command in commands)

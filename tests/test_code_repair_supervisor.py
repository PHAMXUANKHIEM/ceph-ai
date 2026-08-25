from pathlib import Path
from types import SimpleNamespace

from worker import code_repair_supervisor as supervisor
from worker.code_repair_supervisor import Cursor, read_new_errors


def test_first_start_ignores_historical_errors(tmp_path):
    log = tmp_path / "ceph-ai-worker.log"
    log.write_text("ERROR historical\n")
    cursors = {}
    assert read_new_errors([log], cursors, initialize_at_end=True) == []
    log.write_text(log.read_text() + "ERROR new failure\n")
    found = read_new_errors([log], cursors, initialize_at_end=False)
    assert len(found) == 1
    assert "new failure" in found[0]


def test_rotation_reads_replacement_from_start(tmp_path):
    log = tmp_path / "ceph-ai-dashboard.log"
    log.write_text("ERROR after rotation\n")
    cursors = {str(log): Cursor(inode=-1, offset=999)}
    found = read_new_errors([log], cursors, initialize_at_end=False)
    assert "after rotation" in found[0]


def test_supervisor_ignores_its_own_log(tmp_path):
    log = tmp_path / "ceph-ai-code-repair-supervisor.log"
    log.write_text("ERROR recursive failure\n")
    assert read_new_errors([log], {}, initialize_at_end=False) == []


def test_large_backlog_skips_stale_error_and_reads_fresh_tail(tmp_path):
    log = tmp_path / "ceph-ai-watcher.log"
    stale = "ERROR stale detached instance\n"
    log.write_text(stale + ("routine chatter\n" * 30_000) + "ERROR fresh failure\n")
    cursors = {str(log): Cursor(log.stat().st_ino, 0)}

    found = read_new_errors([log], cursors, initialize_at_end=False)

    assert len(found) == 1
    assert "fresh failure" in found[0]
    assert "stale detached instance" not in found[0]
    assert cursors[str(log)].offset == log.stat().st_size


def test_ceph_learning_uses_same_test_deploy_pipeline(monkeypatch, tmp_path):
    verification = supervisor.ceph_learning.VerificationResult("VERIFIED", "ok", (), True)
    candidate = supervisor.ceph_learning.LearningCandidate("f1", "key1", "CEPH evidence", verification)
    captured = {}
    monkeypatch.setattr(supervisor, "read_new_errors", lambda *a, **k: [])
    monkeypatch.setattr(supervisor.ceph_learning, "load_state", lambda p: {"initialized": True, "findings": {}})
    monkeypatch.setattr(supervisor.ceph_learning, "save_state", lambda *a: None)
    monkeypatch.setattr(supervisor.ceph_learning, "next_candidate", lambda seen: candidate)
    monkeypatch.setattr(supervisor.ceph_learning, "mark", lambda state, item, status, **kwargs: captured.setdefault("statuses", []).append(status))
    monkeypatch.setattr(supervisor.settings, "code_repair_cursor_file", str(tmp_path / "cursor.json"))
    monkeypatch.setattr(supervisor.settings, "ceph_capability_learning_state_file", str(tmp_path / "learning.json"))
    monkeypatch.setattr(supervisor.settings, "ceph_capability_learning_enabled", True)
    monkeypatch.setattr(supervisor.settings, "code_repair_auto_enabled", True)
    monkeypatch.setattr(supervisor.settings, "code_repair_push", True)
    monkeypatch.setattr(supervisor.settings, "code_repair_deploy_staging", True)
    monkeypatch.setattr(supervisor.settings, "code_repair_promote_main", True)
    monkeypatch.setattr(supervisor.settings, "code_repair_max_attempts", 3)
    monkeypatch.setattr(supervisor.settings, "code_repair_running_stale_seconds", 3600)

    def fake_run(evidence, config):
        captured["evidence"] = evidence
        captured["config"] = config
        return SimpleNamespace(status="PROMOTED", fingerprint="fp")

    monkeypatch.setattr(supervisor, "run_repair", fake_run)
    supervisor.run_forever(max_iterations=1)

    assert captured["evidence"] == "CEPH evidence"
    assert captured["config"].task_kind == "ceph-capability-learning"
    assert captured["config"].push is True
    assert captured["config"].deploy_staging is True
    assert captured["config"].promote_main is True
    assert captured["statuses"] == ["RUNNING", "LEARNED"]

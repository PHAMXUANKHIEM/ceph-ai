from pathlib import Path

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

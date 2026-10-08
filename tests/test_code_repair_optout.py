"""An operator can switch Code Repair off for good (08/10/2026)."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKER = "/var/lib/ceph-ai/config/code-repair.disabled"


def _deploy_tail() -> str:
    text = (ROOT / "scripts/deploy/restart_container_stack.sh").read_text()
    return text[text.index("systemctl daemon-reload\n# An operator"):text.index("finish_phase", text.index("# An operator"))]


def test_deploy_keeps_code_repair_off_when_the_marker_exists(tmp_path):
    tail = _deploy_tail()
    assert f"if [ -e {MARKER} ]" in tail
    # Run the block with systemctl/install stubbed, with and without the marker.
    stub = tmp_path / "bin"
    stub.mkdir()
    for name in ("systemctl", "install"):
        (stub / name).write_text(f'#!/bin/sh\necho "{name} $*" >> "$LOG"\n')
        (stub / name).chmod(0o755)
    for marker_present in (True, False):
        log = tmp_path / f"log-{marker_present}"
        marker = tmp_path / "code-repair.disabled"
        marker.unlink(missing_ok=True)
        if marker_present:
            marker.write_text("off")
        script = tail.replace(MARKER, str(marker))
        subprocess.run(["bash", "-c", script], check=True, env={"PATH": f"{stub}:/usr/bin:/bin", "LOG": str(log),
                                                                 "REPO_DIR": str(ROOT)})
        calls = log.read_text()
        if marker_present:
            assert "systemctl disable --now ceph-ai-code-repair-supervisor.service" in calls
            assert "enable --now" not in calls
        else:
            assert "systemctl enable --now ceph-ai-code-repair-supervisor.service" in calls


def test_legacy_restart_also_respects_the_marker_and_the_log_is_rotated():
    assert f"if [ -e {MARKER} ]; then" in (ROOT / "scripts/deploy/restart_services.sh").read_text()
    assert "/var/log/ceph-ai-code-repair-supervisor.log" in (ROOT / "scripts/deploy/logrotate/ceph-ai").read_text()

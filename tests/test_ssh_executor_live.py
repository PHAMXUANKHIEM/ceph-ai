"""Real integration test against the actual Ceph lab cluster — no mocking.

Marked `live` (see pyproject.toml). Skips gracefully if the Worker's SSH key
isn't present, matching the pattern used throughout this project.

NOTE (Story 3.2): the lab cluster was torn down by the operator mid-story —
this could NOT be verified against a real host at write time. Runs a
harmless `echo` rather than the real resync_ntp command, so it only needs
*a* reachable node, not a specific NTP daemon. MUST be re-run once a lab
cluster exists again; if it fails, it's telling you something real.
"""

import os

import pytest

from config.settings import settings
from worker.executor.ssh_executor import execute_command

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.path.exists(settings.ssh_key_path),
        reason=f"Worker SSH key not found at {settings.ssh_key_path} — skipping live test",
    ),
]


def test_execute_command_against_real_lab_node():
    host = settings.ceph_mon_nodes.split(",")[0].strip()

    output = execute_command(host, "echo story-3.2-live-check")

    assert "story-3.2-live-check" in output

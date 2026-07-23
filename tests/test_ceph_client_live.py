"""Real integration test against the actual Ceph lab cluster — no mocking.

Marked `live` and excluded from the default `pytest` run (see pyproject.toml
`addopts`) so nobody fires real SSH traffic at lab infrastructure just by
running the normal test suite. Run explicitly with: `pytest -m live`.

Also skips gracefully if the Watcher's SSH key isn't present (e.g. a
different environment without lab access), rather than failing hard.
"""

import os

import pytest

from config.settings import settings
from watcher.ceph_client import query_cluster_health

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.path.exists(settings.ssh_key_path),
        reason=f"Watcher SSH key not found at {settings.ssh_key_path} — skipping live cluster test",
    ),
]


def test_query_cluster_health_against_real_lab_cluster():
    result = query_cluster_health()

    assert result["status"] in {"HEALTH_OK", "HEALTH_WARN", "HEALTH_ERR"}
    assert "checks" in result


def test_real_cluster_currently_reports_mon_clock_skew():
    # Documented in Dev Notes: the lab cluster is currently HEALTH_WARN with
    # a real MON_CLOCK_SKEW on mon2/mon3. If this ever stops being true (the
    # skew gets fixed), this test should be updated/removed rather than
    # treated as a regression.
    result = query_cluster_health()

    assert result["status"] == "HEALTH_WARN"
    assert "MON_CLOCK_SKEW" in result["checks"]

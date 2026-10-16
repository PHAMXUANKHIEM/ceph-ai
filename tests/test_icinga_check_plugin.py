"""deploy/icinga/check_ceph_cluster against a fake `ceph` CLI (plan icinga I1)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "deploy" / "icinga" / "check_ceph_cluster"


def _run(tmp_path, check, response, *extra, returncode=0):
    fake = tmp_path / "ceph"
    fake.write_text("#!/bin/sh\n"
                    f"cat <<'JSON'\n{json.dumps(response)}\nJSON\n"
                    f"exit {returncode}\n")
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    result = subprocess.run([sys.executable, str(PLUGIN), check, "--conf", "c", "--keyring", "k", *extra],
                            capture_output=True, text=True, env=env)
    return result.returncode, result.stdout.splitlines()


def test_health_maps_status_and_lists_the_codes(tmp_path):
    code, lines = _run(tmp_path, "health", {"status": "HEALTH_WARN", "checks": {
        "OSD_DOWN": {"severity": "HEALTH_WARN", "summary": {"message": "1 osds down"}},
        "PG_DEGRADED": {"severity": "HEALTH_WARN", "summary": {"message": "Degraded"}}}})
    assert code == 1 and lines[0].startswith("WARNING - HEALTH_WARN: OSD_DOWN, PG_DEGRADED")
    assert lines[1] == "OSD_DOWN (HEALTH_WARN): 1 osds down"   # the format shared/icinga_events reads


@pytest.mark.parametrize("used, expected", [(50, 0), (80, 1), (90, 2)])
def test_capacity_thresholds(tmp_path, used, expected):
    code, lines = _run(tmp_path, "capacity", {"stats": {"total_bytes": 100, "total_used_raw_bytes": used}})
    assert code == expected and "used_percent=" in lines[0]


@pytest.mark.parametrize("up, expected", [(3, 0), (2, 1), (1, 2)])
def test_osd_down_counts(tmp_path, up, expected):
    code, _ = _run(tmp_path, "osd", {"num_osds": 3, "num_up_osds": up, "num_in_osds": 3})
    assert code == expected


def test_inactive_pgs_are_critical(tmp_path):
    code, lines = _run(tmp_path, "pg", {"num_pgs": 10, "num_pg_by_state": [
        {"name": "active+clean", "num": 8}, {"name": "peering", "num": 2}]})
    assert code == 2 and "2 inactive" in lines[0]


def test_lost_mon_quorum(tmp_path):
    code, lines = _run(tmp_path, "mon", {"monmap": {"mons": [{"name": "a"}, {"name": "b"}, {"name": "c"}]},
                                          "quorum_names": ["a"]})
    assert code == 2 and "out: b, c" in lines[0]


def test_a_failing_ceph_call_is_unknown(tmp_path):
    code, lines = _run(tmp_path, "health", {}, returncode=1)
    assert code == 3 and lines[0].startswith("UNKNOWN")

"""Epic 10 (Ceph Upgrade Test Runner) -- the 7 fixed baseline-file keys and
storage directory, named exactly as in docs/ceph-upgrade-test-cases.md's
"Baseline bat buoc truoc khi test" row.

Moved here (out of dashboard/routes/test_runner.py, where Story 10.2 first
defined these) because Story 10.4's Group B test cases
(worker/executor/test_runner/group_b.py) need to read the actual uploaded
file CONTENT, not just its presence flag, and worker/ must not import from
dashboard/ -- dashboard depends on worker, not the reverse (same layering
shared/cluster_nodes.py already establishes for node config).
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["BASELINE_FILE_KEYS", "BASELINE_FILES_DIR", "baseline_file_path", "read_baseline_text"]

# ceph-aiops project root: shared/test_runner_baselines.py -> shared -> ceph-aiops.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASELINE_FILES_DIR = PROJECT_ROOT / "test_runner_baselines"

# The exact 7 baseline files named in the original 63-test-case document --
# fixed allowlist, not operator-extensible.
BASELINE_FILE_KEYS = (
    "rbd_rep.sha256",
    "cephfs.sha256",
    "s3_manifest.csv",
    "osd_crush_dump_before.json",
    "auth_list_before.txt",
    "config_dump_before.txt",
    "df_before.txt",
)


def baseline_file_path(key: str) -> Path:
    """Path a baseline file was/would be stored at. Does not check
    existence -- callers that need to distinguish "uploaded" from "not
    uploaded" should use read_baseline_text()'s None return instead of
    checking .exists() themselves, keeping that check in one place."""
    return BASELINE_FILES_DIR / key


def read_baseline_text(key: str) -> str | None:
    """The uploaded baseline file's text content, or None if it was never
    uploaded. Every one of the 7 keys is a text file (sha256 manifest, CSV,
    JSON, or plain ceph CLI output) -- none are expected to be binary."""
    path = baseline_file_path(key)
    if not path.exists():
        return None
    return path.read_text()

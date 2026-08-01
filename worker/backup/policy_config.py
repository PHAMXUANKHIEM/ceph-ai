"""Shared loader for `worker/policy/backup_policy.yaml` — used by both
`scheduler.py` (schedule/tracked_images) and `engine.py` (retention/
tracked_images), same "one file, one loader" posture as `worker/policy/
gate.py` for `action_policy.yaml`.
"""

import os

import yaml

POLICY_PATH = os.path.join(os.path.dirname(__file__), "..", "policy", "backup_policy.yaml")


def load_backup_policy() -> dict:
    with open(POLICY_PATH) as f:
        return yaml.safe_load(f)


def backup_targets_from_policy() -> list[dict]:
    """Shared by `engine.py` (RBD backup/retention) and `metadata.py`
    (Story 9.3) — lives here rather than in either module so neither has
    to import the other just for this."""
    return load_backup_policy().get("backup_targets") or []

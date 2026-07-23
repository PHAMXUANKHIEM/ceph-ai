import logging
from pathlib import Path

import yaml

from shared.models import ActionClassification

logger = logging.getLogger(__name__)

_POLICY_PATH = Path(__file__).resolve().parent / "action_policy.yaml"


def _load_action_id_lists() -> tuple[frozenset[str], frozenset[str]]:
    with open(_POLICY_PATH) as f:
        policy = yaml.safe_load(f)
    return frozenset(policy.get("safe") or []), frozenset(policy.get("risky") or [])


def _load_management_action_ids() -> frozenset[str]:
    """Chat-with-AI's own closed action_id enum (dashboard/chat_client.py) —
    see action_policy.yaml's `management_action_ids:` comment for why this
    is separate from worker/llm/router_client.py's VALID_ACTION_IDS
    (Incident-diagnosis enum, loaded from `action_ids:` instead)."""
    with open(_POLICY_PATH) as f:
        policy = yaml.safe_load(f)
    return frozenset(policy.get("management_action_ids") or [])


def _load_cluster_upgrade_action_ids() -> frozenset[str]:
    """Cluster Upgrade feature's own closed action_id enum
    (dashboard/routes/upgrade.py) — see action_policy.yaml's
    `cluster_upgrade_action_ids:` comment for why this is a third family,
    separate from both action_ids and management_action_ids."""
    with open(_POLICY_PATH) as f:
        policy = yaml.safe_load(f)
    return frozenset(policy.get("cluster_upgrade_action_ids") or [])


SAFE_ACTION_IDS, RISKY_ACTION_IDS = _load_action_id_lists()
VALID_MANAGEMENT_ACTION_IDS = _load_management_action_ids()
VALID_CLUSTER_UPGRADE_ACTION_IDS = _load_cluster_upgrade_action_ids()

_CONFLICTING_ACTION_IDS = SAFE_ACTION_IDS & RISKY_ACTION_IDS
if _CONFLICTING_ACTION_IDS:
    logger.warning(
        "action_policy.yaml lists %s in BOTH safe: and risky: — treating as RISKY "
        "(conservative override, AD-5)",
        sorted(_CONFLICTING_ACTION_IDS),
    )


def classify_action(action_id: str) -> ActionClassification:
    """AD-5: conservative by default — SAFE only for an explicit allowlist
    hit that isn't ALSO listed as risky, RISKY for everything else (including
    an action_id that's neither in `safe` nor `risky`, one listed in both, and
    any action_id not recognized at all)."""
    if action_id in SAFE_ACTION_IDS and action_id not in RISKY_ACTION_IDS:
        return ActionClassification.SAFE
    return ActionClassification.RISKY

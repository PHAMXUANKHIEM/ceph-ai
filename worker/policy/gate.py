import logging
from pathlib import Path

import yaml

from shared.models import ActionClassification

logger = logging.getLogger(__name__)

_POLICY_PATH = Path(__file__).resolve().parent / "action_policy.yaml"


def _load_action_id_lists() -> tuple[frozenset[str], frozenset[str], frozenset[str], frozenset[str]]:
    with open(_POLICY_PATH) as f:
        policy = yaml.safe_load(f)
    return (
        frozenset(policy.get("read_only") or []),
        frozenset(policy.get("safe") or []),
        frozenset(policy.get("risky") or []),
        frozenset(policy.get("destructive") or []),
    )


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


def _load_patch_action_ids() -> frozenset[str]:
    """Ceph patch build & deploy pipeline's own closed action_id enum
    (dashboard/routes/patch.py) — see action_policy.yaml's
    `patch_action_ids:` comment for why this is a fourth family."""
    with open(_POLICY_PATH) as f:
        policy = yaml.safe_load(f)
    return frozenset(policy.get("patch_action_ids") or [])


def _load_cluster_deploy_action_ids() -> frozenset[str]:
    """Dựng cụm Ceph tự động feature's own closed action_id enum
    (dashboard/routes/deploy_cluster.py) — see action_policy.yaml's
    `cluster_deploy_action_ids:` comment for why this is a fifth family."""
    with open(_POLICY_PATH) as f:
        policy = yaml.safe_load(f)
    return frozenset(policy.get("cluster_deploy_action_ids") or [])


def _load_volume_perf_action_ids() -> frozenset[str]:
    """Volumes page "Đo hiệu năng tối đa" (load sweep) feature's own closed
    action_id enum (dashboard/routes/volumes.py) — see action_policy.yaml's
    `volume_perf_action_ids:` comment for why this is a seventh family."""
    with open(_POLICY_PATH) as f:
        policy = yaml.safe_load(f)
    return frozenset(policy.get("volume_perf_action_ids") or [])


def _load_backup_action_ids() -> frozenset[str]:
    """Epic 9 Ceph Backup & Disaster Recovery's own closed action_id enum
    (worker/backup/engine.py) — see action_policy.yaml's
    `backup_action_ids:` comment for why this is an eighth family."""
    with open(_POLICY_PATH) as f:
        policy = yaml.safe_load(f)
    return frozenset(policy.get("backup_action_ids") or [])


def _load_device_health_action_ids() -> frozenset[str]:
    """Story C DeviceHealth-driven evacuation's own closed action_id enum
    (watcher/device_health_monitor.py) — see action_policy.yaml's
    `device_health_action_ids:` comment for why this is a ninth family."""
    with open(_POLICY_PATH) as f:
        policy = yaml.safe_load(f)
    return frozenset(policy.get("device_health_action_ids") or [])


def _load_bluestore_action_ids() -> frozenset[str]:
    """BlueStore per-pool omap quick-fix's own closed action_id enum
    (dashboard/routes/nodes.py) — see action_policy.yaml's
    `bluestore_action_ids:` comment for why this is a tenth family."""
    with open(_POLICY_PATH) as f:
        policy = yaml.safe_load(f)
    return frozenset(policy.get("bluestore_action_ids") or [])


READ_ONLY_ACTION_IDS, SAFE_ACTION_IDS, RISKY_ACTION_IDS, DESTRUCTIVE_ACTION_IDS = _load_action_id_lists()
VALID_MANAGEMENT_ACTION_IDS = _load_management_action_ids()
VALID_CLUSTER_UPGRADE_ACTION_IDS = _load_cluster_upgrade_action_ids()
VALID_PATCH_ACTION_IDS = _load_patch_action_ids()
VALID_BACKUP_ACTION_IDS = _load_backup_action_ids()
VALID_CLUSTER_DEPLOY_ACTION_IDS = _load_cluster_deploy_action_ids()
VALID_VOLUME_PERF_ACTION_IDS = _load_volume_perf_action_ids()
VALID_DEVICE_HEALTH_ACTION_IDS = _load_device_health_action_ids()
VALID_BLUESTORE_ACTION_IDS = _load_bluestore_action_ids()

_CONFLICTING_ACTION_IDS = SAFE_ACTION_IDS & RISKY_ACTION_IDS
if _CONFLICTING_ACTION_IDS:
    logger.warning(
        "action_policy.yaml lists %s in BOTH safe: and risky: — treating as RISKY "
        "(conservative override, AD-5)",
        sorted(_CONFLICTING_ACTION_IDS),
    )

# AI roadmap Pha 0.4: DESTRUCTIVE always wins over every other list an
# action_id might mistakenly also appear in (same conservative-override
# spirit as the safe/risky conflict check above, just one level more
# conservative) — a YAML authoring mistake that lists something as both
# `safe:`/`risky:` AND `destructive:` must never let it auto-execute.
_UNSAFE_DESTRUCTIVE_OVERLAP = DESTRUCTIVE_ACTION_IDS & (SAFE_ACTION_IDS | READ_ONLY_ACTION_IDS)
if _UNSAFE_DESTRUCTIVE_OVERLAP:
    logger.warning(
        "action_policy.yaml lists %s in BOTH destructive: and safe:/read_only: — "
        "treating as DESTRUCTIVE (conservative override, AD-5)",
        sorted(_UNSAFE_DESTRUCTIVE_OVERLAP),
    )


def classify_action(action_id: str, session=None) -> ActionClassification:
    """AI roadmap Pha 0.4: 4-tier version of AD-5's original conservative-
    by-default rule. Precedence, most conservative wins:

    1. DESTRUCTIVE — action_id in `destructive:`, regardless of what else
       it's listed under (see _UNSAFE_DESTRUCTIVE_OVERLAP above).
    2. RISKY — action_id in `risky:`, or in BOTH `safe:` and `risky:`
       (original AD-5 conflict rule, unchanged).
    3. SAFE — action_id in `safe:` only.
    4. READ_ONLY — action_id in `read_only:` only.
    5. RISKY — the fail-safe default for anything not recognized in any
       list (original AD-5 default, unchanged: an action_id absent from
       every list, or newly added to `action_ids:` without an explicit
       classification, is never silently auto-run).
    """
    if session is not None:
        # An authenticated admin may explicitly override every runtime tier.
        # The append-only audit row and exact OK confirmation are enforced by
        # the Dashboard route that writes this record.
        from shared.models import ActionPolicyOverride
        override = session.get(ActionPolicyOverride, action_id)
        if override is not None and override.classification in {
            ActionClassification.SAFE.value,
            ActionClassification.RISKY.value,
            ActionClassification.DESTRUCTIVE.value,
        }:
            return ActionClassification(override.classification)
    if action_id in DESTRUCTIVE_ACTION_IDS:
        return ActionClassification.DESTRUCTIVE
    if action_id in RISKY_ACTION_IDS:
        return ActionClassification.RISKY
    if action_id in SAFE_ACTION_IDS:
        return ActionClassification.SAFE
    if action_id in READ_ONLY_ACTION_IDS:
        return ActionClassification.READ_ONLY
    return ActionClassification.RISKY

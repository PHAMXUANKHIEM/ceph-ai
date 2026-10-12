"""scripts/action_safety_inventory.py (Plan readiness-gap-review R4)."""

import importlib.util
from pathlib import Path

from worker.policy import gate

_SPEC = importlib.util.spec_from_file_location(
    "action_safety_inventory", Path(__file__).resolve().parents[1] / "scripts" / "action_safety_inventory.py")
inventory_script = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(inventory_script)


def test_every_policy_action_is_listed_once_with_its_classification():
    rows = inventory_script.inventory()
    ids = [row["action_id"] for row in rows]
    assert len(ids) == len(set(ids))
    assert set(gate.SAFE_ACTION_IDS) | set(gate.RISKY_ACTION_IDS) | set(gate.DESTRUCTIVE_ACTION_IDS) <= set(ids)
    assert all(row["classification"] in {"READ_ONLY", "SAFE", "RISKY", "DESTRUCTIVE"} for row in rows)


def test_lifecycle_phases_are_resolved_like_the_worker_does():
    rows = {row["action_id"]: row for row in inventory_script.inventory()}
    assert rows["replace_failed_osd"]["phases"][0] == "osd_preflight"
    assert rows["replace_failed_osd"]["preflight"] and rows["finish_replace_osd"]["postcheck"]
    assert "| `replace_failed_osd` | DESTRUCTIVE |" in inventory_script.markdown(list(rows.values()))

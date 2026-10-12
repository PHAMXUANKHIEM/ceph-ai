#!/usr/bin/env python3
"""Every action Ceph AI can run, with the safety steps it declares (Plan readiness-gap-review R4).

Read-only; no database. Lists each action id from worker/policy/action_policy.yaml with its
classification and what its playbook contract (worker/policy/playbook_registry.py) or, for
cluster lifecycle actions, its phases (worker/executor/cluster_deploy.py) declare. "Declared"
is the point: an executor may check more inside; this inventory shows what the policy layer
can rely on.

    python scripts/action_safety_inventory.py            # markdown table
    python scripts/action_safety_inventory.py --json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from worker.executor import cluster_deploy, commands  # noqa: E402
from worker.policy import gate, playbook_registry  # noqa: E402


def inventory() -> list[dict]:
    ids = (set(gate.READ_ONLY_ACTION_IDS) | set(gate.SAFE_ACTION_IDS) | set(gate.RISKY_ACTION_IDS)
           | set(gate.DESTRUCTIVE_ACTION_IDS) | set(gate.CLUSTER_LIFECYCLE_ACTION_IDS))
    for name in dir(gate):
        if name.startswith("VALID_") and name.endswith("_ACTION_IDS"):
            ids |= set(getattr(gate, name))
    def phases_of(action_id: str) -> list:
        # The same lookup cluster_deploy.run() does.
        return (cluster_deploy._PHASES_BY_ACTION_ID.get(action_id) or cluster_deploy._node_removal_phases(action_id)
                or cluster_deploy._osd_replacement_phases(action_id) or [])

    rows = []
    for action_id in sorted(ids):
        contract = playbook_registry.get_contract(action_id)
        phases = [phase[0] for phase in phases_of(action_id)]
        lifecycle = action_id in gate.CLUSTER_LIFECYCLE_ACTION_IDS
        rows.append({
            "action_id": action_id,
            "classification": gate.classify_action(action_id).value,
            "lifecycle": lifecycle,
            "has_command": commands.has_command(action_id) or lifecycle,
            "contract": contract is not None,
            "preflight": bool(contract and contract.preflight) or any("preflight" in p or "check" in p for p in phases),
            "postcheck": bool(contract and contract.postcheck)
            or any(p.endswith("_up") or word in p for p in phases for word in ("wait", "verify", "health")),
            "rollback": bool(contract and contract.rollback),
            "max_targets": contract.max_targets if contract else None,
            "phases": phases,
        })
    return rows


def markdown(rows: list[dict]) -> str:
    mark = {True: "✓", False: "—"}
    lines = ["| Hành động | Mức | Vòng đời | Contract | Preflight | Postcheck | Rollback | Max targets |",
             "|---|---|---|---|---|---|---|---|"]
    for row in rows:
        lines.append(f"| `{row['action_id']}` | {row['classification']} | {mark[row['lifecycle']]} | "
                     f"{mark[row['contract']]} | {mark[row['preflight']]} | {mark[row['postcheck']]} | "
                     f"{mark[row['rollback']]} | {row['max_targets'] or '—'} |")
    return "\n".join(lines)


if __name__ == "__main__":
    data = inventory()
    print(json.dumps(data, indent=1) if "--json" in sys.argv else markdown(data))

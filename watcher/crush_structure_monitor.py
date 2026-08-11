"""CRUSH structure capture (Epic 12, Story 12.1, F1) -- periodically snapshots
the cluster's CRUSH tree (Root/Rack/Host/OSD + Weight, from `ceph osd crush
dump`) so Story 12.2 (skew detection) and Story 12.3 (Dashboard tree page)
have a durable, always-fresh structure to read from `shared/db`.

Pure collector -- mirrors `watcher/node_metrics.py`'s role, NOT
`watcher/node_health_monitor.py`'s: this module never creates an Incident,
never sends Telegram, never imports `shared.audit` (AD-25). It only ever
touches `CrushStructureSnapshot`.

2026-08 -- NOT yet verified against a real cluster this session (no live
cluster access at implementation time, same disclosure posture
`watcher/osd_latency_monitor.py`'s own docstring already established for
`ceph osd perf`). `ceph osd crush dump`'s JSON shape used here (`devices`,
`buckets` with `id`/`name`/`type_name`/`weight`/`items[].id`) is Ceph's
long-stable, publicly documented CRUSH map format, but has never been
parsed into a structured tree anywhere else in this codebase before --
`watcher/ceph_client.py::list_osds()` parses `ceph osd tree` instead (a
DIFFERENT, flatter command with no Weight), so there was no existing parser
to verify this against. Parsing here is deliberately defensive (`.get()`
throughout, malformed/missing fields skipped rather than raised) precisely
because of this.

2026-08-07 (Story 12.2 fix, found while reading this module's own code
before building the Skew feature) -- `_build_tree()`'s OSD leaf branch used
to always set `"weight": None`, discarding the leaf's real Weight (it only
exists on its PARENT bucket's `items[].weight` entry, not on the device
entry itself). Fixed by threading the parent item's `weight` down through
`resolve()`'s recursion. A `CrushStructureSnapshot` row written BEFORE this
fix still has `weight: None` on every OSD leaf in its stored `tree_json` --
`crush_skew_monitor.py` must tolerate that (treat as "no Weight data this
row", not crash) rather than assume every row has the fixed shape.
"""

from __future__ import annotations

import json

from shared import db
from shared.models import CrushStructureSnapshot
from watcher import ceph_client
from watcher.ceph_client import CephQueryError


def capture_crush_structure() -> dict | None:
    """Runs `ceph osd crush dump` and returns the parsed tree (see
    `_build_tree`), or `None` if the query itself failed -- never raises,
    same best-effort posture as every other Watcher scan in this codebase."""
    try:
        _, payload = ceph_client.run_ceph_json_command("ceph osd crush dump")
    except CephQueryError:
        return None
    if not isinstance(payload, dict):
        return None
    return _build_tree(payload)


def _build_tree(crush_dump: dict) -> dict:
    """Turns `ceph osd crush dump`'s flat `devices`/`buckets` representation
    into a nested Root->Rack->Host->OSD tree.

    `devices` is a flat list of OSD leaves (`{"id": int, "name": "osd.N",
    "class": ...}`). `buckets` is a flat list of non-leaf nodes (root/rack/
    host/...), each `{"id": <negative int>, "name": str, "type_name": str,
    "weight": int (CRUSH's fixed-point representation, 65536 == weight
    1.0), "items": [{"id": int, "weight": int, ...}, ...]}` -- an item's
    `id` is either another bucket's `id` (negative, resolved recursively)
    or a device's `id` (non-negative, a leaf OSD). A cluster can have more
    than one root; this returns ALL of them under a synthetic top-level
    `roots` list rather than assuming exactly one.
    """
    rules = crush_dump.get("rules")
    normalized_rules = []
    if isinstance(rules, list):
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            steps = rule.get("steps")
            normalized_rules.append({
                "rule_id": rule.get("rule_id"), "rule_name": rule.get("rule_name"),
                "type": rule.get("type"), "min_size": rule.get("min_size"), "max_size": rule.get("max_size"),
                "steps": [{key: step.get(key) for key in ("op", "item", "item_name", "num", "type") if key in step}
                          for step in steps or [] if isinstance(step, dict)],
            })
    devices = crush_dump.get("devices")
    buckets = crush_dump.get("buckets")
    if not isinstance(devices, list) or not isinstance(buckets, list):
        return {"roots": [], "rules": normalized_rules}

    device_names = {
        d["id"]: d.get("name") for d in devices if isinstance(d, dict) and "id" in d
    }
    buckets_by_id = {
        b["id"]: b for b in buckets if isinstance(b, dict) and "id" in b
    }

    def resolve(node_id: int, item_weight: int | None = None) -> dict | None:
        bucket = buckets_by_id.get(node_id)
        if bucket is not None:
            children = []
            for item in bucket.get("items") or []:
                if not isinstance(item, dict) or "id" not in item:
                    continue
                child_weight = item.get("weight")
                if not isinstance(child_weight, int):
                    child_weight = None
                child = resolve(item["id"], child_weight)
                if child is not None:
                    children.append(child)
            return {
                "id": node_id,
                "name": bucket.get("name"),
                "type": bucket.get("type_name"),
                "weight": bucket.get("weight"),
                "children": children,
            }
        if node_id in device_names:
            # 2026-08-07 (Story 12.2): an OSD leaf carries no `weight` of its
            # own in `ceph osd crush dump` -- its Weight only exists on the
            # PARENT bucket's `items[].weight` entry that references this
            # leaf's id (`item_weight`, passed down by the recursive call
            # above). Without this, every OSD leaf's `weight` was always
            # `None`, and crush_skew_monitor.py's Skew formula (FR-8) has no
            # per-OSD Weight to compare against its siblings.
            return {
                "id": node_id,
                "name": device_names[node_id],
                "type": "osd",
                "weight": item_weight,
                "children": [],
            }
        return None

    roots = [
        resolve(b["id"])
        for b in buckets
        if isinstance(b, dict) and b.get("type_name") == "root" and "id" in b
    ]
    return {"roots": [r for r in roots if r is not None], "rules": normalized_rules}


def _sort_tree(node: dict) -> dict:
    """Recursively sorts every `children` list by `id` -- `json.dumps(...,
    sort_keys=True)` alone only sorts an object's OWN keys, never the
    element order of a list, so two structurally-identical CRUSH dumps
    fetched a moment apart could otherwise compare as "different" purely
    because Ceph returned `items`/`buckets` in a different order (AD-26)."""
    sorted_children = sorted(
        (_sort_tree(child) for child in node.get("children") or []),
        key=lambda c: c.get("id") if c.get("id") is not None else 0,
    )
    return {**node, "children": sorted_children}


def _canonicalize(tree: dict) -> str:
    """Deterministic JSON string for the same logical tree regardless of
    the order Ceph happened to return buckets/items in this call (AD-26)."""
    roots = sorted(
        (_sort_tree(r) for r in tree.get("roots") or []),
        key=lambda r: r.get("id") if r.get("id") is not None else 0,
    )
    rules = sorted(
        (rule for rule in tree.get("rules") or [] if isinstance(rule, dict)),
        key=lambda rule: (rule.get("rule_id") is None, rule.get("rule_id") or 0, rule.get("rule_name") or ""),
    )
    return json.dumps({"roots": roots, "rules": rules}, sort_keys=True)


def _compute_diff(old_tree: dict, new_tree: dict) -> dict:
    """Bucket/OSD add/remove/reweight delta between two trees -- an OSD's
    up/down status is not part of either tree in the first place (this
    module never reads it), so it can never appear in the diff (AC #4)."""

    def flatten(node: dict, out: dict[int, dict]) -> None:
        node_id = node.get("id")
        if node_id is not None:
            out[node_id] = {"name": node.get("name"), "type": node.get("type"), "weight": node.get("weight")}
        for child in node.get("children") or []:
            flatten(child, out)

    old_nodes: dict[int, dict] = {}
    new_nodes: dict[int, dict] = {}
    for root in old_tree.get("roots") or []:
        flatten(root, old_nodes)
    for root in new_tree.get("roots") or []:
        flatten(root, new_nodes)

    added = [new_nodes[i] | {"id": i} for i in new_nodes if i not in old_nodes]
    removed = [old_nodes[i] | {"id": i} for i in old_nodes if i not in new_nodes]
    reweighted = [
        {"id": i, "name": new_nodes[i]["name"], "type": new_nodes[i]["type"],
         "old_weight": old_nodes[i]["weight"], "new_weight": new_nodes[i]["weight"]}
        for i in new_nodes
        if i in old_nodes and old_nodes[i]["weight"] != new_nodes[i]["weight"]
    ]
    return {"added": added, "removed": removed, "reweighted": reweighted}


def scan_and_store() -> None:
    """One scan cycle -- called from `watcher/main.py::run()` on its own
    `crush_scan_interval_seconds` cadence. No-op if the query itself failed
    (AC #5). Writes a new `CrushStructureSnapshot` only when the canonical
    form differs from the single most-recent row (AC #2); the very first
    snapshot ever taken always gets `diff_json=None` (AC #1) since there is
    no prior row to diff against."""
    new_tree = capture_crush_structure()
    if new_tree is None:
        return

    with db.SessionLocal() as session:
        latest = (
            session.query(CrushStructureSnapshot)
            .order_by(CrushStructureSnapshot.created_at.desc())
            .first()
        )
        new_canonical = _canonicalize(new_tree)

        if latest is None:
            session.add(CrushStructureSnapshot(tree_json=new_canonical, diff_json=None))
            session.commit()
            return

        old_tree = json.loads(latest.tree_json)
        if _canonicalize(old_tree) == new_canonical:
            return

        diff = _compute_diff(old_tree, new_tree)
        session.add(
            CrushStructureSnapshot(tree_json=new_canonical, diff_json=json.dumps(diff))
        )
        session.commit()

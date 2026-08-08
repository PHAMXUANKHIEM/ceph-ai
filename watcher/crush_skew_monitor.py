"""CRUSH data-distribution Skew detection + alerting (Epic 12, Story 12.2, F4)
-- reads the two tables `watcher/crush_structure_monitor.py`/
`watcher/crush_distribution_monitor.py` (Story 12.1) already collect and
flags an OSD or Host carrying disproportionately more (or less) data than
its CRUSH Weight implies, relative to its SIBLINGS under the same immediate
parent Bucket.

Unlike its two Story 12.1 sibling modules, this one IS an alerting module
(same role as `watcher/node_health_monitor.py`/`watcher/osd_latency_monitor.py`,
NOT `watcher/node_metrics.py`) -- it creates/resolves `Incident`+`Action`
rows and sends Telegram, reusing the SAME PENDING_APPROVAL/`investigate_manually`
lifecycle those two modules already established (AD-30). It never writes to
`CrushStructureSnapshot`/`CrushOsdDistribution` -- those are Story 12.1's
tables, read-only from here.

Skew formula (PRD FR-8, verbatim -- do not invent a different one):

    skew = (actual_ratio - expected_ratio) / expected_ratio

`expected_ratio` of an OSD/Host = its own Weight / SUM of Weight of its
siblings under the SAME immediate parent Bucket. `actual_ratio` = its own
`bytes_used` (or `pgs`) / SUM of `bytes_used` (or `pgs`) of that same
sibling group. Both sides are always compared LOCALLY within one sibling
group, never against cluster-wide totals (AD-28). Two independent signals
are computed per entity every scan: `CRUSH_SKEW_USE` (bytes-based) and
`CRUSH_SKEW_PG` (PG-count-based) -- each with its own consecutive-scans
streak and its own Incident/ceph_code family, exactly like two unrelated
checks (AD-28's own reasoning for why this is 2 signals, not 1 combined
score).

Scope is deliberately limited to OSD and Host entities only (AD-28's
ceph_code shape is `<osd_id|host>`) -- Rack/Root-level Skew is out of scope
for v1 even though the formula generalizes to any tree depth.

Weight=0-but-actual>0 (e.g. an OSD/Host being drained) is a documented
edge case (PRD FR-8 AC #2): the formula's denominator would be zero, so
this is special-cased to the maximum skew (1.0 == 100%) instead of raising
or silently skipping.
"""

from __future__ import annotations

import json
from datetime import datetime

from shared import audit, db
from shared.models import (
    Action,
    ActionStatus,
    CrushOsdDistribution,
    CrushStructureSnapshot,
    Incident,
    IncidentStatus,
)
from shared.telegram_alerts import send_crush_skew_alert
from worker.policy import gate

CRUSH_SKEW_USE_PREFIX = "CRUSH_SKEW_USE:"
CRUSH_SKEW_PG_PREFIX = "CRUSH_SKEW_PG:"

# No automated remediation exists for "this OSD/Host is carrying more data
# than its Weight implies" (could be a legitimate in-progress rebalance, a
# misconfigured Weight, or a genuine hardware imbalance) -- same "no
# Command, operator investigates" posture as
# watcher/osd_latency_monitor.py's own OSD_LATENCY_ACTION_ID. Already
# registered in worker/policy/action_policy.yaml's `action_ids` list and
# defaults to RISKY (absent from the `safe` list) -- no policy file change
# needed for this story.
SKEW_ACTION_ID = "investigate_manually"

# Internal tuning constants, not config/settings.py fields -- same
# "operational tuning, not per-deployment config" convention as
# watcher/osd_latency_monitor.py's OUTLIER_LATENCY_RATIO/
# CONSECUTIVE_SCANS_REQUIRED. No real cluster was available to derive these
# from live data at story-creation/implementation time (PRD §11 Open
# Question #1, Architecture Deferred) -- see this story's Dev Notes for the
# reasoning behind these specific starting values.
#
# A relative deviation of 50% from the expected LOCAL share (within the
# sibling group) is treated as worth investigating. Chosen higher than
# osd_latency_monitor.py's OUTLIER_LATENCY_RATIO=3.0 because that constant
# is a multiplier on an instantaneous metric (latency), while this one is a
# relative-percentage deviation on a slowly-drifting metric (data
# distribution) -- the two are not directly comparable units.
SKEW_RATIO_THRESHOLD = 0.5

# Higher than osd_latency_monitor.py's CONSECUTIVE_SCANS_REQUIRED=2: a
# legitimate rebalance (after an OSD/Weight change) produces a REAL Skew
# for many consecutive scans while data migrates -- 3 in a row (3 minutes
# at the default 60s crush_scan_interval_seconds) still catches genuine
# problems quickly without flagging the very first minute of every normal
# rebalance. Kept as 2 SEPARATE constants (not 1 shared one) so USE and PG
# sensitivity can be tuned independently later without touching code
# structure, matching this story's own Dev Notes.
CONSECUTIVE_USE_SCANS_REQUIRED = 3
CONSECUTIVE_PG_SCANS_REQUIRED = 3

# Mirrors watcher/main.py::_RECOVERABLE_STATUSES / the identical copies in
# every other watcher/*_monitor.py module -- kept as its own copy rather
# than a cross-import, same "independent modules" reasoning all of them
# already document.
_RECOVERABLE_STATUSES = {
    IncidentStatus.NEW.value,
    IncidentStatus.DIAGNOSING.value,
    IncidentStatus.PENDING_APPROVAL.value,
    IncidentStatus.APPROVED.value,
    IncidentStatus.EXECUTING.value,
    IncidentStatus.FAILED.value,
}

# Module-level, process-lifetime state -- one entry per entity key ever
# scanned since this Watcher process started, same judgment call as
# osd_latency_monitor.py's/node_health_monitor.py's own streak dicts.
# Two SEPARATE dicts (not one) because the USE and PG signals for the SAME
# entity can be at different points in their own streak independently
# (AD-28). Keys are string-typed ("osd:<id>" / "host:<name>"), not the
# raw osd_id/host, so an OSD id and a host name can never collide in the
# same dict.
_consecutive_use_skew_scans: dict[str, int] = {}
_consecutive_pg_skew_scans: dict[str, int] = {}


def _entity_key(kind: str, entity_id) -> str:
    return f"{kind}:{entity_id}"


def ceph_code_for(prefix: str, entity_id) -> str:
    return f"{prefix}{entity_id}"


def _skew_ratio(
    own_actual: int | None,
    siblings_actual_sum: int,
    own_weight: int | None,
    siblings_weight_sum: int,
) -> float | None:
    """FR-8's formula, verbatim: `(actual_ratio - expected_ratio) /
    expected_ratio`, both ratios computed LOCALLY within the sibling group
    (never against a cluster-wide total).

    Returns `None` if `own_actual` is missing (no data for this entity this
    scan -- skip it, do not fabricate a Skew value). Returns `1.0` (max
    skew) if the expected share is zero (`own_weight` is 0/None, or the
    whole sibling group's Weight sums to 0) but the entity still has real
    actual data -- the formula's denominator would otherwise be a division
    by zero (PRD FR-8 AC #2, a draining OSD/Host is the canonical example).
    Returns `0.0` in the same zero-expected-share case if `own_actual` is
    also 0 -- both sides being zero is not an anomaly.
    """
    if own_actual is None:
        return None

    own_weight = own_weight or 0
    if own_weight <= 0 or siblings_weight_sum <= 0:
        return 1.0 if own_actual > 0 else 0.0

    expected = own_weight / siblings_weight_sum
    actual = (own_actual / siblings_actual_sum) if siblings_actual_sum else 0.0
    return (actual - expected) / expected


def _flatten_sibling_groups(tree: dict) -> list[list[dict]]:
    """Returns every Bucket's own `children` list found anywhere in `tree`
    (each is one sibling group, per FR-8's "cùng cấp dưới CÙNG MỘT Bucket
    cha") -- walks the whole tree recursively starting from `tree["roots"]`.
    Groups with fewer than 2 children are still returned (a lone child's
    Skew is still well-defined -- it simply has no siblings to differ
    from, expected_ratio == actual_ratio == 1.0 in that case, skew == 0)."""
    groups: list[list[dict]] = []

    def walk(node: dict) -> None:
        children = node.get("children") or []
        if children:
            groups.append(children)
        for child in children:
            walk(child)

    for root in tree.get("roots") or []:
        walk(root)
    return groups


def _osd_actual(node: dict, distribution: dict[int, dict], field: str) -> int | None:
    """An OSD leaf's actual metric comes straight from its
    `CrushOsdDistribution` row. An OSD present in the Structure tree but
    ALREADY ABSENT from `distribution` (Story 12.1's `sync_distribution()`
    deletes a row the moment an OSD is confirmed gone -- see that module's
    own docstring) is treated as having no data at all here, even though it
    may still linger in a not-yet-refreshed Snapshot for up to one more
    scan cycle (the two tables are written independently, in sequence, in
    the same tick -- see this story's Dev Notes) -- Distribution is the
    authoritative "does this OSD still really exist" source for this
    module, not Structure."""
    row = distribution.get(node.get("id"))
    if row is None:
        return None
    return row.get(field)


def _host_actual_and_weight(
    node: dict, distribution: dict[int, dict], field: str
) -> tuple[int | None, int | None]:
    """A Host's actual metric is the SUM of `field` over every OSD
    descendant that still has a row in `distribution` (AD-27: Host/Rack
    skew must derive from summed raw bytes, never a precomputed
    percentage). An OSD descendant missing from `distribution` is simply
    excluded from the sum (not treated as 0), matching `_osd_actual`'s own
    "Distribution is authoritative" reasoning. Returns `(None, weight)` if
    NO descendant OSD has any data at all this scan (nothing to sum -- the
    caller must treat this the same as "no data" for an OSD leaf, not as a
    real 0).

    The Host's own Weight is read directly from its Bucket node's `weight`
    field (populated by Story 12.1's `_build_tree()` from `ceph osd crush
    dump`'s own Bucket-level `weight`) -- NOT re-derived by summing child
    Weights, since CRUSH already guarantees a well-formed Bucket's declared
    weight tracks its children (and re-deriving it would silently paper
    over a genuinely misconfigured Bucket weight instead of surfacing it)."""

    def descendant_osds(n: dict) -> list[dict]:
        if n.get("type") == "osd":
            return [n]
        result: list[dict] = []
        for child in n.get("children") or []:
            result.extend(descendant_osds(child))
        return result

    total = None
    for osd_node in descendant_osds(node):
        value = _osd_actual(osd_node, distribution, field)
        if value is None:
            continue
        total = (total or 0) + value

    return total, node.get("weight")


def check_crush_skew() -> dict[str, dict]:
    """Reads the single most-recent `CrushStructureSnapshot` and the full
    current `CrushOsdDistribution` table, computes both Skew signals for
    every OSD/Host entity found, and returns `{ceph_code: detail}` for
    every entity whose signal has stayed at/above `SKEW_RATIO_THRESHOLD`
    (absolute value -- a large NEGATIVE skew, i.e. carrying much LESS than
    expected, is just as worth flagging as a large positive one) for
    `CONSECUTIVE_USE_SCANS_REQUIRED`/`CONSECUTIVE_PG_SCANS_REQUIRED` scans
    in a row. No-op (returns `{}`) if no Snapshot exists yet -- never
    raises, same best-effort posture as every other Watcher scan module."""
    with db.SessionLocal() as session:
        latest_snapshot = (
            session.query(CrushStructureSnapshot)
            .order_by(CrushStructureSnapshot.created_at.desc())
            .first()
        )
        if latest_snapshot is None:
            return {}
        tree = json.loads(latest_snapshot.tree_json)

        distribution: dict[int, dict] = {
            row.osd_id: {
                "host": row.host,
                "bytes_used": row.bytes_used,
                "bytes_total": row.bytes_total,
                "pgs": row.pgs,
            }
            for row in session.query(CrushOsdDistribution).all()
        }

    flagged: dict[str, dict] = {}

    for group in _flatten_sibling_groups(tree):
        for entity_type in ("osd", "host"):
            siblings = [n for n in group if n.get("type") == entity_type]
            if not siblings:
                continue

            for field, prefix, streaks, required in (
                ("bytes_used", CRUSH_SKEW_USE_PREFIX, _consecutive_use_skew_scans, CONSECUTIVE_USE_SCANS_REQUIRED),
                ("pgs", CRUSH_SKEW_PG_PREFIX, _consecutive_pg_skew_scans, CONSECUTIVE_PG_SCANS_REQUIRED),
            ):
                actuals: dict[int, int | None] = {}
                weights: dict[int, int | None] = {}
                for node in siblings:
                    if entity_type == "osd":
                        actuals[node["id"]] = _osd_actual(node, distribution, field)
                        weights[node["id"]] = node.get("weight")
                    else:
                        actual, weight = _host_actual_and_weight(node, distribution, field)
                        actuals[node["id"]] = actual
                        weights[node["id"]] = weight

                # Only entities with actual data THIS scan participate in
                # either sum -- a sibling missing from `distribution`
                # entirely (removed from the cluster, or a lagging
                # Structure Snapshot for up to 1 tick, see this story's Dev
                # Notes) must not inflate the expected-share denominator
                # for its still-present siblings just because its stale
                # Weight is still sitting in the tree.
                present_ids = {node_id for node_id, v in actuals.items() if v is not None}
                siblings_actual_sum = sum(actuals[i] for i in present_ids)
                siblings_weight_sum = sum((weights[i] or 0) for i in present_ids)

                for node in siblings:
                    node_id = node["id"]
                    own_actual = actuals[node_id]
                    entity_label = node["id"] if entity_type == "osd" else node.get("name")
                    key = _entity_key(entity_type, entity_label)

                    skew = _skew_ratio(own_actual, siblings_actual_sum, weights[node_id], siblings_weight_sum)
                    if skew is None:
                        # No data for this entity/field this scan -- do not
                        # advance OR reset its streak, same "skip silently"
                        # posture as a failed poll elsewhere in this
                        # codebase (a transient gap must not either fake a
                        # recovery or fake a new consecutive scan).
                        continue

                    is_over = abs(skew) >= SKEW_RATIO_THRESHOLD
                    if is_over:
                        streaks[key] = streaks.get(key, 0) + 1
                    else:
                        streaks[key] = 0

                    if streaks[key] >= required:
                        ceph_code = ceph_code_for(prefix, entity_label)
                        flagged[ceph_code] = {
                            "entity_type": entity_type,
                            "entity_id": entity_label,
                            "signal": "USE" if field == "bytes_used" else "PG",
                            "skew": skew,
                            "own_actual": own_actual,
                            "siblings_actual_sum": siblings_actual_sum,
                            "own_weight": weights[node_id] or 0,
                            "siblings_weight_sum": siblings_weight_sum,
                            "consecutive_scans": streaks[key],
                        }

    return flagged


def _rationale_for(detail: dict) -> str:
    entity_label = (
        f"osd.{detail['entity_id']}" if detail["entity_type"] == "osd" else f"host {detail['entity_id']}"
    )
    signal_label = "%%USE" if detail["signal"] == "USE" else "số PG"
    return (
        f"{entity_label} lệch tải {detail['skew'] * 100:.0f}% so với tỷ trọng kỳ vọng theo Weight "
        f"(tính theo {signal_label}, so cục bộ trong nhóm anh em cùng Bucket cha), "
        f"lặp lại {detail['consecutive_scans']} lần quét liên tiếp — có thể do cấu hình Weight sai, "
        f"cụm đang rebalance kéo dài bất thường, hoặc phần cứng gánh tải không đều."
    )


def _entity_label_for_alert(detail: dict) -> str:
    if detail["entity_type"] == "osd":
        return f"osd.{detail['entity_id']}"
    return f"host {detail['entity_id']}"


def create_or_resolve_crush_skew_incidents(current: dict[str, dict]) -> None:
    """Same shape as watcher/osd_latency_monitor.py::
    create_or_resolve_osd_latency_incidents -- creates a PENDING_APPROVAL
    Incident+Action(investigate_manually) for every newly-flagged ceph_code
    not already open, and resolves any open CRUSH_SKEW_USE:/CRUSH_SKEW_PG:
    Incident whose ceph_code dropped out of `current` (covers BOTH "Skew
    dropped back under threshold" and "entity removed from the cluster"
    the exact same way -- an entity gone from the cluster never appears in
    `current` in the first place, AD-30, no separate code path needed).
    Sends a Telegram alert (shared/telegram_alerts.py::send_crush_skew_alert,
    the Phần cứng channel, AD-31) for each NEWLY created Incident only."""
    with db.SessionLocal() as session:
        open_incidents = (
            session.query(Incident)
            .filter(
                Incident.ceph_code.like(f"{CRUSH_SKEW_USE_PREFIX}%")
                | Incident.ceph_code.like(f"{CRUSH_SKEW_PG_PREFIX}%")
            )
            .filter(Incident.status.in_(_RECOVERABLE_STATUSES))
            .all()
        )
        open_codes = {incident.ceph_code for incident in open_incidents}

        for incident in open_incidents:
            if incident.ceph_code not in current:
                incident.status = IncidentStatus.RESOLVED.value

        for ceph_code, detail in current.items():
            if ceph_code in open_codes:
                continue  # already has an open Incident — don't duplicate/re-alert

            rationale = _rationale_for(detail)
            incident = Incident(
                ceph_code=ceph_code,
                status=IncidentStatus.PENDING_APPROVAL.value,
                detected_at=datetime.utcnow(),
                log_excerpt=rationale,
            )
            session.add(incident)
            session.flush()  # assigns incident.id, needed by the Action FK below

            action = Action(
                incident_id=incident.id,
                action_id=SKEW_ACTION_ID,
                classification=gate.classify_action(SKEW_ACTION_ID).value,
                status=ActionStatus.PENDING_APPROVAL.value,
                rationale=rationale,
                # No automated target -- investigate_manually has no
                # Command regardless (has_command() is False for it, same
                # as osd_latency_monitor.py/node_health_monitor.py's
                # identical comment).
                target_nodes=json.dumps([]),
                action_params=json.dumps(
                    {
                        "entity_type": detail["entity_type"],
                        "entity_id": detail["entity_id"],
                        "signal": detail["signal"],
                        "skew": detail["skew"],
                    }
                ),
            )
            session.add(action)
            session.flush()

            audit.record(
                session,
                incident_id=incident.id,
                action_id=action.id,
                event_type=audit.EVENT_RISKY_ACTION_PENDING_APPROVAL,
                actor=audit.ACTOR_SYSTEM,
            )

            send_crush_skew_alert(detail["signal"], _entity_label_for_alert(detail), rationale)
        session.commit()

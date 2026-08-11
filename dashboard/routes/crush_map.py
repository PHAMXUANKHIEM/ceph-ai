"""Epic 12, Story 12.3 (F2) -- admin-only Dashboard page + JSON API for the
interactive CRUSH tree. Pure read layer over what Story 12.1's Watcher
modules already persist (`CrushStructureSnapshot`/`CrushOsdDistribution`) --
this module never writes to either table. Independent of Story 12.2
(skew detection/Incidents) -- see epics.md's "12.2 và 12.3 độc lập với
nhau".
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import and_, or_

from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared import db
from shared.models import CrushOsdDistribution, CrushStructureSnapshot

router = APIRouter()
templates = make_templates()

# AD-29/FR-4: PRD leaves the "recently changed" auto-hide threshold as an
# open [ASSUMPTION] -- this module owns the concrete number, same
# "dev proposes a constant where PRD left it open" precedent Story 12.2
# followed for its Skew ratio/CONSECUTIVE_SCANS_REQUIRED.
RECENT_CHANGE_HOURS = 24

# CRUSH's fixed-point Weight representation (65536 == weight 1.0) -- same
# constant `watcher/crush_structure_monitor.py`/`crush_skew_monitor.py`
# implicitly rely on via the raw `ceph osd crush dump` weight field.
CRUSH_WEIGHT_SCALE = 65536

DEFAULT_HISTORY_LIMIT = 20
MAX_HISTORY_LIMIT = 100


def _require_admin_privilege(user: str) -> None:
    if not auth.is_admin_user(user):
        raise HTTPException(
            status_code=403,
            detail="Chỉ tài khoản admin mới được phép xem CRUSH Map",
        )


def _normalized_weight(weight: int | float | None) -> float | None:
    if not isinstance(weight, (int, float)):
        return None
    return weight / CRUSH_WEIGHT_SCALE


def _load_distribution_by_osd_id() -> dict[int, dict]:
    with db.SessionLocal() as session:
        rows = session.query(CrushOsdDistribution).all()
        return {
            row.osd_id: {
                "host": row.host,
                "bytes_used": row.bytes_used,
                "bytes_total": row.bytes_total,
                "pgs": row.pgs,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
            for row in rows
        }


def _recent_change_map(diff: dict | None, changed_at: str) -> dict[int, dict]:
    """Only `added`/`reweighted` entries can be attached to a node still
    present in the current tree -- a `removed` entry references a node that,
    by definition, no longer exists to attach a badge to (it only ever
    surfaces via the history detail view, see `_snapshot_detail`).

    `changed_at` (the owning Snapshot's own `created_at`, ISO string) is
    attached to every entry -- Story 12.3 AC #6 requires the badge to
    include "thời điểm đổi" (when the change happened), not just what
    changed; every entry in one Snapshot's diff shares the same moment by
    definition (one Snapshot = one point in time)."""
    if not diff:
        return {}
    changes: dict[int, dict] = {}
    for item in diff.get("added") or []:
        node_id = item.get("id")
        if node_id is not None:
            changes[node_id] = {
                "kind": "added",
                "name": item.get("name"),
                "type": item.get("type"),
                "changed_at": changed_at,
            }
    for item in diff.get("reweighted") or []:
        node_id = item.get("id")
        if node_id is not None:
            changes[node_id] = {
                "kind": "reweighted",
                "name": item.get("name"),
                "type": item.get("type"),
                "old_weight": item.get("old_weight"),
                "new_weight": item.get("new_weight"),
                "changed_at": changed_at,
            }
    return changes


def _sum_field(nodes: list[dict], key: str) -> tuple[int | None, int]:
    """Sums `key` over `nodes`, skipping any node where that field is
    `None` (AD-27: a child with no data must be excluded from the sum, not
    treated as zero, or the parent's total would be understated). Returns
    `(sum_or_None, contributor_count)`."""
    values = [n[key] for n in nodes if n.get(key) is not None]
    if not values:
        return None, 0
    return sum(values), len(values)


def _augment_node(node: dict, distribution: dict[int, dict], changes: dict[int, dict]) -> dict:
    node_id = node.get("id")
    node_type = node.get("type")
    recent_change = changes.get(node_id)

    if node_type == "osd":
        row = distribution.get(node_id)
        return {
            "id": node_id,
            "name": node.get("name"),
            "type": node_type,
            "weight": node.get("weight"),
            "weight_normalized": _normalized_weight(node.get("weight")),
            "host": row["host"] if row else None,
            "bytes_used": row["bytes_used"] if row else None,
            "bytes_total": row["bytes_total"] if row else None,
            "pgs": row["pgs"] if row else None,
            "has_distribution_data": row is not None,
            "partial_distribution_data": False,
            "recent_change": recent_change,
            "children": [],
        }

    children = [_augment_node(child, distribution, changes) for child in node.get("children") or []]
    bytes_used, used_contributors = _sum_field(children, "bytes_used")
    bytes_total, total_contributors = _sum_field(children, "bytes_total")
    pgs, pgs_contributors = _sum_field(children, "pgs")
    max_contributors = max(used_contributors, total_contributors, pgs_contributors)
    return {
        "id": node_id,
        "name": node.get("name"),
        "type": node_type,
        "weight": node.get("weight"),
        "weight_normalized": _normalized_weight(node.get("weight")),
        "bytes_used": bytes_used,
        "bytes_total": bytes_total,
        "pgs": pgs,
        "has_distribution_data": max_contributors > 0,
        "partial_distribution_data": 0 < max_contributors < len(children),
        "recent_change": recent_change,
        "children": children,
    }


def _tree_has_osd(node: dict) -> bool:
    if node.get("type") == "osd":
        return True
    return any(_tree_has_osd(child) for child in node.get("children") or [])


def _build_tree_response(latest: CrushStructureSnapshot) -> dict:
    tree = json.loads(latest.tree_json)
    roots = tree.get("roots") or []
    rules = tree.get("rules") or []

    if not any(_tree_has_osd(root) for root in roots):
        return {
            "state": "empty_cluster",
            "snapshot_id": latest.id,
            "created_at": latest.created_at.isoformat(),
            "rules": rules,
        }

    diff = json.loads(latest.diff_json) if latest.diff_json else None
    is_recent = (
        diff is not None
        and datetime.utcnow() - latest.created_at <= timedelta(hours=RECENT_CHANGE_HOURS)
    )
    changes = _recent_change_map(diff, latest.created_at.isoformat()) if is_recent else {}
    distribution = _load_distribution_by_osd_id()

    return {
        "state": "ok",
        "snapshot_id": latest.id,
        "created_at": latest.created_at.isoformat(),
        "roots": [_augment_node(root, distribution, changes) for root in roots],
        "rules": rules,
    }


@router.get("/crush-map", response_class=HTMLResponse)
async def crush_map_page(request: Request, user: str = Depends(require_login)):
    """Standalone admin-only page — same posture as `/users`/`/telegram-alerts`:
    a non-admin typing the URL directly gets 403, not just a hidden nav link."""
    _require_admin_privilege(user)
    return templates.TemplateResponse(
        request,
        "crush_map.html",
        {"user": user, "is_admin": True},
    )


@router.get("/api/crush-map/tree")
async def crush_map_tree_api(user: str = Depends(require_login)):
    _require_admin_privilege(user)

    with db.SessionLocal() as session:
        latest = (
            session.query(CrushStructureSnapshot)
            .order_by(CrushStructureSnapshot.created_at.desc())
            .first()
        )
        if latest is None:
            return {"state": "no_snapshot_yet"}
        return _build_tree_response(latest)


@router.get("/api/crush-map/history")
async def crush_map_history_api(
    user: str = Depends(require_login),
    limit: int = Query(DEFAULT_HISTORY_LIMIT, ge=1, le=MAX_HISTORY_LIMIT),
    before: str | None = Query(None),
):
    """Newest-first, simple cursor pagination via `before` (an opaque
    `<created_at ISO>|<id>` cursor from the last item of the previous page
    — pass it straight back to fetch the next older page). The `id` half
    is a tie-breaker: `created_at` alone is not unique enough to paginate
    on safely (two rows can share the exact same timestamp at a page
    boundary), so both `ORDER BY` and the cursor filter use
    `(created_at, id)` together, not `created_at` alone. Only rows with a
    real `diff_json` are "a time the structure changed" (FR-5/FR62) — the
    very first snapshot ever taken has `diff_json=None` (no baseline to
    diff against) and is deliberately excluded here, same distinction
    `crush_structure_monitor.py::scan_and_store` itself draws."""
    _require_admin_privilege(user)

    with db.SessionLocal() as session:
        query = session.query(CrushStructureSnapshot).filter(
            CrushStructureSnapshot.diff_json.isnot(None)
        )
        if before:
            before_ts, _, before_id = before.rpartition("|")
            try:
                before_dt = datetime.fromisoformat(before_ts)
            except ValueError:
                raise HTTPException(status_code=400, detail="Tham số 'before' không hợp lệ")
            if not before_id:
                raise HTTPException(status_code=400, detail="Tham số 'before' không hợp lệ")
            query = query.filter(
                or_(
                    CrushStructureSnapshot.created_at < before_dt,
                    and_(
                        CrushStructureSnapshot.created_at == before_dt,
                        CrushStructureSnapshot.id < before_id,
                    ),
                )
            )

        rows = (
            query.order_by(CrushStructureSnapshot.created_at.desc(), CrushStructureSnapshot.id.desc())
            .limit(limit)
            .all()
        )

        items = []
        for row in rows:
            diff = json.loads(row.diff_json) if row.diff_json else {}
            items.append(
                {
                    "id": row.id,
                    "created_at": row.created_at.isoformat(),
                    "added_count": len(diff.get("added") or []),
                    "removed_count": len(diff.get("removed") or []),
                    "reweighted_count": len(diff.get("reweighted") or []),
                }
            )

        next_before = f"{rows[-1].created_at.isoformat()}|{rows[-1].id}" if len(items) == limit else None
        return {"items": items, "next_before": next_before}


@router.get("/api/crush-map/history/{snapshot_id}")
async def crush_map_history_detail_api(snapshot_id: str, user: str = Depends(require_login)):
    _require_admin_privilege(user)

    with db.SessionLocal() as session:
        row = session.get(CrushStructureSnapshot, snapshot_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy bản ghi lịch sử này")
        diff = json.loads(row.diff_json) if row.diff_json else {"added": [], "removed": [], "reweighted": []}
        return {
            "id": row.id,
            "created_at": row.created_at.isoformat(),
            "added": diff.get("added") or [],
            "removed": diff.get("removed") or [],
            "reweighted": diff.get("reweighted") or [],
        }

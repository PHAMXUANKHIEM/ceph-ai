"""OSD data-distribution collection (Epic 12, Story 12.1, F3) -- periodically
records each OSD's actual capacity usage and PG count from `ceph osd df`,
so Story 12.2 (skew detection) can compare it against the Weight-based
expectation `watcher/crush_structure_monitor.py` captures, and Story 12.3
(Dashboard tree page) can overlay it on the tree.

Pure collector (AD-25) -- never creates an Incident, never sends Telegram,
never imports `shared.audit`. Only ever touches `CrushOsdDistribution`.

2026-08 -- `ceph osd df --format json`'s `nodes[]` entries (`id`, `kb`,
`kb_used`, `pgs`) are Ceph's long-stable, publicly documented per-OSD
`osd df` fields (the same `PGS`/`%USE`/`SIZE` columns the plain-text `ceph
osd df` table has always shown) -- NOT yet verified against a real cluster
this session (no live cluster access at implementation time, same
disclosure posture `watcher/osd_latency_monitor.py`'s own docstring already
established for `ceph osd perf`). Parsing is deliberately defensive
(`.get()` throughout, an OSD entry missing an expected field is skipped
rather than raising) precisely because of this.

AD-25b: this single `ceph osd df` call replaces what the original PRD draft
scoped as a SEPARATE, slower `ceph pg dump` call for PG count -- `ceph osd
df` already reports `pgs` per OSD in the very same response, so there is
no second cadence/setting for this feature.
"""

from __future__ import annotations

from shared import db
from shared.models import CrushOsdDistribution
from watcher import ceph_client
from watcher.ceph_client import CephQueryError


def collect_osd_distribution() -> dict[int, dict] | None:
    """Runs `ceph osd df` and returns `{osd_id: {"host": str | None,
    "bytes_used": int | None, "bytes_total": int | None, "pgs": int |
    None}}` for every OSD entry found. Returns `None` (not an empty dict)
    if the query itself failed -- the caller must be able to tell "the
    scan failed, leave existing data alone" apart from "the scan
    succeeded and found zero OSDs"."""
    try:
        _, payload = ceph_client.run_ceph_json_command("ceph osd df")
    except CephQueryError:
        return None
    if not isinstance(payload, dict):
        return None
    nodes = payload.get("nodes")
    if not isinstance(nodes, list):
        return None

    host_by_osd_id = {o["osd_id"]: o.get("crush_host") for o in ceph_client.list_osds()}

    result: dict[int, dict] = {}
    for node in nodes:
        if not isinstance(node, dict) or "id" not in node:
            continue
        osd_id = node["id"]
        kb = node.get("kb")
        kb_used = node.get("kb_used")
        result[osd_id] = {
            "host": host_by_osd_id.get(osd_id),
            "bytes_used": int(kb_used) * 1024 if isinstance(kb_used, (int, float)) else None,
            "bytes_total": int(kb) * 1024 if isinstance(kb, (int, float)) else None,
            "pgs": node.get("pgs") if isinstance(node.get("pgs"), int) else None,
        }
    return result


def sync_distribution() -> None:
    """One scan cycle -- called from `watcher/main.py::run()` on its own
    `crush_scan_interval_seconds` cadence (shared with
    `crush_structure_monitor.py::scan_and_store`, AD-25b).

    A FAILED scan (`collect_osd_distribution()` returns `None`) leaves
    every existing row untouched -- returns immediately, no writes at all
    (AC #5). A SUCCESSFUL scan upserts every OSD it saw (one row per
    `osd_id`, all 3 numbers + `updated_at` written together since they
    come from the same call) AND deletes any existing row whose `osd_id`
    was NOT in this successful scan's result -- that OSD has genuinely
    left the cluster, as opposed to a scan that failed to reach it at all
    (AC #6)."""
    current = collect_osd_distribution()
    if current is None:
        return

    with db.SessionLocal() as session:
        existing_ids = {
            row.osd_id for row in session.query(CrushOsdDistribution.osd_id).all()
        }

        for osd_id, detail in current.items():
            row = session.get(CrushOsdDistribution, osd_id)
            if row is None:
                row = CrushOsdDistribution(osd_id=osd_id)
                session.add(row)
            row.host = detail["host"]
            row.bytes_used = detail["bytes_used"]
            row.bytes_total = detail["bytes_total"]
            row.pgs = detail["pgs"]

        removed_ids = existing_ids - set(current.keys())
        if removed_ids:
            session.query(CrushOsdDistribution).filter(
                CrushOsdDistribution.osd_id.in_(removed_ids)
            ).delete(synchronize_session=False)

        session.commit()

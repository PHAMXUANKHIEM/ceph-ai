"""Read-only query runner backed by the Watcher's persisted snapshots.

This adapter is deliberately not allowed to fall back to SSH. A missing or
expired snapshot is returned as an explicit unavailable result so the caller
can show a stale/loading state instead of blocking the Chat request.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from config.settings import settings
from shared.cluster_snapshot import (
    DEFAULT_MAX_STALE_SECONDS,
    is_refreshing,
    read_section_snapshot,
    read_snapshot,
)

from .tool_registry import FIXED_READ_ONLY_TOOL_REGISTRY


_SECTION_TO_TOOL = {
    "get_osd_tree": "crush",
    "get_pool_list": "pools",
    "get_pg_stat": "pgs",
    "get_osd_stat": "status",
    "get_df": "status",
    "get_mon_stat": "status",
    "get_cluster_status": "status",
    "get_health_detail": "health",
}


def _as_dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _meta(cluster_id: str, snapshot: Mapping[str, Any] | None, *, available: bool) -> dict[str, Any]:
    snapshot = snapshot or {}
    partial_errors = snapshot.get("partial_errors", {})
    if not isinstance(partial_errors, Mapping):
        partial_errors = {}
    return {
        "cluster_id": cluster_id,
        "generation": snapshot.get("generation", 0),
        "collected_at": snapshot.get("collected_at"),
        "published_at": snapshot.get("published_at"),
        "age_seconds": snapshot.get("age_seconds"),
        "stale": bool(snapshot.get("stale", True)),
        "refreshing": bool(snapshot.get("refreshing", False)),
        "available": bool(available and snapshot.get("section_available", True)),
        "partial": bool(partial_errors) or not available,
        "partial_errors": dict(partial_errors),
        "last_error": snapshot.get("last_error"),
        "source": snapshot.get("collector"),
    }


def _missing(cluster_id: str, *, refreshing: bool | None = None, reason: str = "snapshot_missing") -> dict[str, Any]:
    if refreshing is None:
        refreshing = is_refreshing(cluster_id)
    return {
        "data": None,
        "meta": {
            "cluster_id": cluster_id,
            "generation": 0,
            "collected_at": None,
            "published_at": None,
            "age_seconds": None,
            "stale": True,
            "refreshing": refreshing,
            "available": False,
            "partial": True,
            "partial_errors": {"snapshot": reason},
            "last_error": reason,
            "source": None,
        },
    }


class SnapshotQueryRunner:
    """Callable runner matching ``execute_query_plan``'s scoped contract."""

    def __init__(
        self,
        *,
        stale_after_seconds: int | None = None,
        max_stale_seconds: int = DEFAULT_MAX_STALE_SECONDS,
    ) -> None:
        self.stale_after_seconds = (
            settings.ceph_snapshot_max_age if stale_after_seconds is None else stale_after_seconds
        )
        self.max_stale_seconds = max_stale_seconds

    def _read_main(self, cluster_id: str) -> dict | None:
        return read_snapshot(
            cluster_id,
            stale_after_seconds=self.stale_after_seconds,
            max_stale_seconds=self.max_stale_seconds,
        )

    def _read_section(self, cluster_id: str, section: str) -> dict | None:
        return read_section_snapshot(
            cluster_id,
            section,
            stale_after_seconds=self.stale_after_seconds,
            max_stale_seconds=self.max_stale_seconds,
        )

    def __call__(self, tool_name: str, _arguments: dict[str, Any], cluster_id: str) -> dict[str, Any]:
        if tool_name not in FIXED_READ_ONLY_TOOL_REGISTRY:
            raise ValueError(f"snapshot runner only supports fixed read-only tools: {tool_name!r}")
        cluster_id = str(cluster_id or "").strip()
        if not cluster_id:
            raise ValueError("cluster_id is required")

        main = self._read_main(cluster_id)
        section_name = _SECTION_TO_TOOL[tool_name]
        section = self._read_section(cluster_id, section_name) if section_name != "health" else main
        selected = section or main
        if selected is None:
            return _missing(cluster_id)
        if str(selected.get("cluster_id") or cluster_id) != cluster_id:
            return _missing(cluster_id, reason="cluster_scope_mismatch")

        available = bool(selected.get("section_available", True))
        if section_name == "status" and section is None:
            available = False
        status = _as_dict(selected.get("status")) if section_name == "status" else {}
        health = _as_dict(main.get("health")) if main else {}

        if tool_name == "get_cluster_status":
            data = _as_dict(selected.get("status")) if selected is not None else {}
            if not data and main:
                data = _as_dict(main.get("health"))
        elif tool_name == "get_health_detail":
            data = {
                "status": health.get("status") or main.get("health_status"),
                "checks": health.get("checks") or _as_dict(main.get("health_checks")),
            }
            available = bool(main and main.get("health_available", True))
        elif tool_name == "get_osd_stat":
            data = {"osdmap": _as_dict(status.get("osdmap"))}
            available = bool(status.get("osdmap"))
        elif tool_name == "get_osd_tree":
            data = selected.get("crush") if section_name == "crush" else None
        elif tool_name == "get_pool_list":
            data = selected.get("pools") if section_name == "pools" else None
        elif tool_name == "get_pg_stat":
            data = selected.get("pgs") if section_name == "pgs" else status.get("pgmap")
            available = data is not None
        elif tool_name == "get_df":
            data = {"pgmap": _as_dict(status.get("pgmap"))}
            available = bool(data["pgmap"])
        elif tool_name == "get_mon_stat":
            data = {
                "monmap": _as_dict(status.get("monmap")),
                "quorum_names": status.get("quorum_names", []),
            }
            available = bool(data["monmap"])
        else:  # guarded by registry above; keep fail-closed for future edits
            raise ValueError(f"no snapshot mapping for {tool_name!r}")

        meta = _meta(cluster_id, selected, available=available)
        # A section can be fresh while the health snapshot is stale, and vice
        # versa. Preserve that fact in the response instead of hiding it.
        if main and selected is not main:
            meta["health_stale"] = bool(main.get("stale", True))
            meta["health_age_seconds"] = main.get("age_seconds")
        return {"data": data, "meta": meta}

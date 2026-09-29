"""Generate a secret-free architecture profile for this installation.

The profile combines process settings with per-cluster database configuration.
It describes enabled/configured paths; it does not probe external services.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from functools import lru_cache
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml  # type: ignore[import-untyped]


STREAM_ARCHITECTURE_NODES: dict[str, tuple[str, ...]] = {
    "browser": ("browser.ceph_jinja", "browser.react", "dashboard.installation_stream"),
    "dashboard": ("dashboard.app", "dashboard.installation_stream", "dashboard.auth", "dashboard.cluster_scope"),
    "database": ("data.sqlalchemy", "data.cluster_registry", "shared.installation_profile"),
    "watcher": (
        "watcher.main", "watcher.collectors", "watcher.ceph_client",
        "watcher.block_storage_dependencies",
    ),
    "rabbit": ("event.incident_queue", "external.rabbitmq"),
    "worker": ("worker.main", "worker.executor", "worker.backup"),
    "ceph": ("external.ceph", "watcher.ceph_client"),
    "object_storage": ("dashboard.object_storage", "external.rgw_s3", "worker.rgw_audit"),
    "openstack": ("dashboard.cinder_backups", "external.openstack"),
    "ai_provider": ("dashboard.chat", "worker.diagnosis", "ai.shared_client", "external.ai_provider"),
    "backup": ("dashboard.backup_restore", "worker.backup", "external.backup_targets"),
    "telegram": ("dashboard.alerting", "dashboard.telegram_approval", "external.telegram"),
    "log_intelligence": ("dashboard.log_intelligence", "watcher.collectors", "external.logs"),
    "vitastor": ("dashboard.vitastor", "watcher.vitastor", "external.vitastor"),
    "federated_identity": ("dashboard.federated_iam", "shared.directory_identity", "worker.federated_iam", "external.identity_provider"),
    "vault_monitoring": ("external.vault_identity", "shared.directory_identity"),
}


@lru_cache(maxsize=2)
def _read_architecture_manifest(path: str) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    if not isinstance(loaded, dict) or not isinstance(loaded.get("nodes"), dict) or not isinstance(loaded.get("flows"), dict):
        raise ValueError("Architecture manifest must define nodes and flows mappings")
    return loaded


def build_stream_architecture_links(manifest_path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Map visible Stream nodes to reviewed graph flows, relationships, and tests."""
    if manifest_path is None:
        repository_root = Path(__file__).resolve().parents[1]
        candidates = (
            repository_root / "tests/architecture/graph.yaml",
            repository_root / "docs/architecture/graph.yaml",
        )
        manifest_path = next((candidate for candidate in candidates if candidate.is_file()), candidates[0])
    manifest = _read_architecture_manifest(str(manifest_path.resolve()))
    manifest_nodes = manifest["nodes"]
    flows = manifest["flows"]
    edges = manifest.get("edges", [])
    result: dict[str, dict[str, Any]] = {}

    for stream_id, architecture_ids in STREAM_ARCHITECTURE_NODES.items():
        missing = [node_id for node_id in architecture_ids if node_id not in manifest_nodes]
        if missing:
            raise ValueError(f"Stream node {stream_id} references missing architecture nodes: {', '.join(missing)}")
        matching_flows = [
            (flow_id, flow)
            for flow_id, flow in flows.items()
            if set(architecture_ids).intersection(flow.get("nodes", []))
        ]
        related_tests: set[str] = set()
        for node_id in architecture_ids:
            related_tests.update(manifest_nodes[node_id].get("tests", []) or [])
        for _, flow in matching_flows:
            related_tests.update(flow.get("tests", []) or [])
        relationships = [
            {"from": edge[0], "kind": edge[2], "to": edge[1]}
            for edge in edges
            if len(edge) >= 3 and (edge[0] in architecture_ids or edge[1] in architecture_ids)
        ]
        result[stream_id] = {
            "architecture_ids": list(architecture_ids),
            "flows": [
                {"id": flow_id, "criticality": flow.get("criticality", "unknown")}
                for flow_id, flow in matching_flows
            ],
            "tests": sorted(related_tests),
            "relationships": relationships,
        }
    return result


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _present(value: Any) -> bool:
    return bool(str(value or "").strip())


def _hosts(value: Any) -> list[str]:
    """Return unique non-empty CSV entries for counting only; never serialize them."""
    if not _present(value):
        return []
    return list(dict.fromkeys(part.strip() for part in str(value).split(",") if part.strip()))


def _integration(status: str, **details: Any) -> dict[str, Any]:
    return {"status": status, **details}


def build_installation_profile(
    settings: Any,
    ceph_clusters: Iterable[Any],
    vitastor_clusters: Iterable[Any] = (),
    identity_providers: Iterable[Any] = (),
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic, credential-free installation inventory."""
    ceph_rows = list(ceph_clusters)
    vita_rows = list(vitastor_clusters)
    providers = list(identity_providers)
    active_ceph = [row for row in ceph_rows if bool(_get(row, "is_active", True))]
    active_vita = [row for row in vita_rows if bool(_get(row, "is_active", True))]

    cluster_summaries: list[dict[str, Any]] = []
    for index, row in enumerate(ceph_rows, start=1):
        roles = {
            role: len(_hosts(_get(row, field, "")))
            for role, field in (
                ("mon", "ceph_mon_nodes"),
                ("mgr", "ceph_mgr_nodes"),
                ("osd", "ceph_osd_nodes"),
                ("rgw", "ceph_rgw_nodes"),
            )
        }
        transport = str(_get(row, "backup_transport", "") or "").strip().lower()
        cluster_summaries.append(
            {
                "label": f"Ceph cluster {index}",
                "active": bool(_get(row, "is_active", True)),
                "default": bool(_get(row, "is_default", False)),
                "execution_mode": str(_get(row, "ceph_exec_mode", "unknown") or "unknown"),
                "node_counts": roles,
                "backup": bool(
                    _get(row, "backup_enabled", False)
                    and _present(_get(row, "backup_tracked_images", ""))
                    and transport in {"ssh", "s3"}
                ),
            }
        )

    global_rgws = _get(settings, "ceph_rgw_nodes", "")
    rgw_configured = any(roles["rgw"] for row in cluster_summaries if row["active"] for roles in [row["node_counts"]])
    rgw_configured = rgw_configured or _present(global_rgws) or _present(_get(settings, "ceph_rgw_s3_endpoint", ""))

    openstack_configured = any(
        bool(_hosts(_get(row, "openstack_controller_nodes", "")) or _hosts(_get(row, "openstack_compute_nodes", "")))
        for row in active_ceph
    )
    global_backup_transports = [
        str(_get(settings, f"backup_target_{slot}_transport", "") or "").strip().lower()
        for slot in ("a", "b")
    ]
    backup_configured = any(
        transport in {"ssh", "s3"} for transport in global_backup_transports
    ) or any(row["backup"] for row in cluster_summaries)

    ai_flags = (
        "router_enabled",
        "codex_chat_enabled",
        "claude_chat_enabled",
        "dual_ai_fallback_enabled",
    )
    ai_configured = any(bool(_get(settings, key, False)) for key in ai_flags)
    telegram_channels = (
        ("backup", "telegram_backup_enabled", "telegram_backup_bot_token", "telegram_backup_chat_id"),
        ("incident", "telegram_incident_enabled", "telegram_incident_bot_token", "telegram_incident_chat_id"),
        ("node", "telegram_node_enabled", "telegram_node_bot_token", "telegram_node_chat_id"),
        ("rgw", "telegram_rgw_enabled", "telegram_rgw_bot_token", "telegram_rgw_chat_id"),
        ("vault", "telegram_vault_enabled", "telegram_vault_bot_token", "telegram_vault_chat_id"),
        ("chatbox", "telegram_chatbox_enabled", "telegram_chatbox_bot_token", "telegram_chatbox_chat_id"),
    )
    enabled_telegram = [
        name
        for name, enabled_field, token_field, chat_field in telegram_channels
        if bool(_get(settings, enabled_field, False))
        and _present(_get(settings, token_field, ""))
        and _present(_get(settings, chat_field, ""))
    ]
    telegram_configured = bool(enabled_telegram) or any(
        bool(_get(row, "is_active", True))
        and bool(_get(row, "telegram_enabled", False))
        and _present(_get(row, "telegram_bot_token", ""))
        and _present(_get(row, "telegram_chat_id", ""))
        for row in ceph_rows
    )

    log_enabled = bool(_get(settings, "log_intel_enabled", False))
    log_source = str(_get(settings, "log_intel_source", "ssh") or "ssh").strip().lower()
    log_configured = log_enabled and (
        (log_source == "loki" and _present(_get(settings, "log_intel_loki_url", "")))
        or (log_source == "elasticsearch" and _present(_get(settings, "log_intel_elasticsearch_url", "")))
        or (log_source == "ssh" and any(_hosts(_get(row, "ceph_mon_nodes", "")) for row in active_ceph))
    )

    provider_types: dict[str, int] = {}
    for provider in providers:
        if bool(_get(provider, "enabled", False)):
            provider_type = str(_get(provider, "provider_type", "unknown") or "unknown").lower()
            provider_types[provider_type] = provider_types.get(provider_type, 0) + 1
    federation_configured = bool(provider_types)
    vault_monitor = bool(_get(settings, "vault_monitor_enabled", False)) and _present(_get(settings, "vault_addr", ""))

    features = {
        "ceph": _integration("configured" if active_ceph else "not_configured", active_clusters=len(active_ceph)),
        "object_storage": _integration("configured" if rgw_configured else "not_configured"),
        "openstack": _integration("configured" if openstack_configured else "not_configured"),
        "ai_provider": _integration("configured" if ai_configured else "not_configured"),
        "backup": _integration("configured" if backup_configured else "not_configured"),
        "telegram": _integration(
            "configured" if telegram_configured else "not_configured",
            listener_enabled=bool(_get(settings, "telegram_listener_enabled", False)),
            configured_channels=enabled_telegram,
        ),
        "log_intelligence": _integration(
            "configured" if log_configured else ("disabled" if not log_enabled else "not_configured"),
            enabled=log_enabled,
            source=log_source if log_source in {"ssh", "loki", "elasticsearch"} else "other",
        ),
        "vitastor": _integration("configured" if active_vita else "not_configured", active_clusters=len(active_vita)),
        "federated_identity": _integration(
            "configured" if federation_configured else "not_configured",
            enabled_provider_types=provider_types,
            worker_role_mapping_support=["oidc"],
        ),
        "vault_monitoring": _integration("configured" if vault_monitor else ("disabled" if not bool(_get(settings, "vault_monitor_enabled", False)) else "not_configured")),
    }
    modes = sorted({row["execution_mode"] for row in cluster_summaries if row["active"]})
    return {
        "schema_version": 1,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "source": "local settings and database; no external service probing",
        "secrets_included": False,
        "ceph_execution_modes": modes,
        "clusters": cluster_summaries,
        "vitastor": {"active_clusters": len(active_vita)},
        "features": features,
        "architecture": build_stream_architecture_links(),
    }


def render_mermaid(profile: dict[str, Any]) -> str:
    """Render a compact installation-specific runtime flow."""
    features = profile["features"]
    lines = [
        "flowchart LR",
        "  browser[Operator browser] --> dashboard[Ceph-AI Dashboard]",
        "  dashboard --> db[(SQL database)]",
        "  watcher[Watcher] --> ceph[(Ceph clusters)]",
        "  watcher --> db",
        "  watcher --> mq[(RabbitMQ)]",
        "  mq --> worker[Worker]",
        "  worker --> db",
        "  worker --> ceph",
    ]
    optional_edges = {
        "object_storage": ("rgw[(RGW / S3)]", "dashboard --> rgw", "worker -. optional RGW jobs .-> rgw"),
        "openstack": ("openstack[(OpenStack)]", "dashboard --> openstack", "worker -. approved Cinder actions .-> openstack"),
        "ai_provider": ("ai[AI provider]", "dashboard -. chat/diagnosis .-> ai", "worker -. diagnosis .-> ai"),
        "backup": ("backup[(Configured backup targets)]", "worker --> backup"),
        "telegram": ("telegram[(Telegram)]", "worker -. notifications .-> telegram", "watcher -. enabled alerts .-> telegram"),
        "log_intelligence": ("logs[(Configured log source)]", "dashboard --> logs", "watcher --> logs"),
        "vitastor": ("vita[(Vitastor / etcd)]", "vita_monitor[Vitastor monitor] --> vita", "dashboard --> vita"),
        "federated_identity": ("identity[(Configured identity provider)]", "dashboard --> identity", "worker -. OIDC role mapping .-> rgw"),
        "vault_monitoring": ("vault[(Vault)]", "watcher -. configured monitoring .-> vault"),
    }
    declarations: set[str] = set()
    for key, candidate_edges in optional_edges.items():
        feature = features[key]
        if feature["status"] != "configured":
            continue
        # A generated flow uses fixed labels only; no configured endpoint, host,
        # credential, cluster name, or provider-supplied text enters Mermaid.
        declarations.add(candidate_edges[0])
        lines.extend(f"  {edge}" for edge in candidate_edges[1:])
    if features["federated_identity"]["status"] == "configured" and features["object_storage"]["status"] == "configured":
        declarations.add("rgw[(RGW / S3)]")
    lines[1:1] = [f"  {node}" for node in sorted(declarations)]
    return "\n".join(lines) + "\n"


def _load_installation_data() -> tuple[Any, list[Any], list[Any], list[Any]]:
    from config.settings import settings
    from shared import db
    from shared.models import Cluster, RgwFederatedIdentityProvider, VitastorCluster
    from sqlalchemy import select

    with db.SessionLocal() as session:
        ceph_clusters = list(session.scalars(select(Cluster).order_by(Cluster.is_default.desc(), Cluster.id)))
        vitastor_clusters = list(session.scalars(select(VitastorCluster).order_by(VitastorCluster.id)))
        providers = list(session.scalars(select(RgwFederatedIdentityProvider).order_by(RgwFederatedIdentityProvider.id)))
    return settings, ceph_clusters, vitastor_clusters, providers


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Ghi profile JSON với quyền file 0600; mặc định in stdout")
    parser.add_argument("--mermaid-output", type=Path, help="Tùy chọn ghi sơ đồ Mermaid, quyền file 0600")
    args = parser.parse_args(argv)
    try:
        settings, clusters, vitastor, providers = _load_installation_data()
    except Exception as exc:
        parser.error(f"Không thể đọc đầy đủ cấu hình và database; không tạo profile thiếu dữ liệu ({type(exc).__name__})")
    profile = build_installation_profile(settings, clusters, vitastor, providers)
    serialized = json.dumps(profile, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        _write_private(args.output, serialized)
    else:
        print(serialized, end="")
    if args.mermaid_output:
        _write_private(args.mermaid_output, render_mermaid(profile))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

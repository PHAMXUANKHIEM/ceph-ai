"""Stable identity helpers for Vitastor connection records.

The display name is an operator label, not a cluster identity.  A native
Vitastor cluster is identified by its Etcd keyspace: the normalized Etcd
endpoints together with the configured prefix.  This lets an operator rename
the same cluster without creating a second dashboard record.
"""

from __future__ import annotations

from urllib.parse import urlsplit


def _normalize_endpoint(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    parsed = urlsplit(value if "://" in value else f"http://{value}")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return ""
    try:
        port = parsed.port or 2379
    except ValueError:
        return ""
    return f"{host}:{port}"


def cluster_identity(values) -> tuple:
    """Return the logical identity represented by a connection form/model."""
    def field(name: str, default: str = "") -> str:
        if isinstance(values, dict):
            return str(values.get(name, default) or default)
        return str(getattr(values, name, default) or default)

    endpoints = tuple(sorted({
        normalized
        for item in field("etcd_address").split(",")
        if (normalized := _normalize_endpoint(item))
    }))
    prefix = field("etcd_prefix", "/vitastor").strip().rstrip("/") or "/"
    if endpoints:
        return ("etcd", endpoints, prefix)
    config_path = field("config_path").strip()
    return ("config", config_path, prefix)


def same_cluster(existing, values) -> bool:
    """Compare only backend identity; names and management hosts may change."""
    return cluster_identity(existing) == cluster_identity(values)

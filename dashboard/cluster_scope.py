"""Resolve the Ceph connection selected by the current dashboard session."""

from threading import Lock
from time import monotonic

from fastapi import HTTPException, Request

from shared import db
from shared.clusters import ensure_default_cluster, list_active_clusters
from shared.cluster_nodes import resolve_ssh_creds
from shared.models import Cluster


# Realtime pages resolve the same active cluster on every snapshot read. A
# very short process-local cache collapses a burst from 1/5/10 browser tabs
# into one database read without becoming a second source of cluster state.
# The session factory identity is part of the cache ownership so test/runtime
# database rebinding can never reuse objects loaded from another database.
_CLUSTER_CACHE_TTL_SECONDS = 1.0
_CLUSTER_CACHE_LOCK = Lock()
_cluster_cache: tuple[int, float, tuple[Cluster, ...], str] | None = None


def clear_cluster_selection_cache() -> None:
    """Invalidate the bounded cluster-selection cache after cluster writes."""
    global _cluster_cache
    with _CLUSTER_CACHE_LOCK:
        _cluster_cache = None


def _active_clusters() -> tuple[list[Cluster], Cluster]:
    global _cluster_cache
    owner = id(db.SessionLocal)
    now = monotonic()
    cached = _cluster_cache
    if cached is not None and cached[0] == owner and cached[1] > now:
        clusters = list(cached[2])
        default = next(cluster for cluster in clusters if cluster.id == cached[3])
        return clusters, default

    with _CLUSTER_CACHE_LOCK:
        now = monotonic()
        cached = _cluster_cache
        if cached is not None and cached[0] == owner and cached[1] > now:
            clusters = list(cached[2])
            default = next(cluster for cluster in clusters if cluster.id == cached[3])
            return clusters, default
        with db.SessionLocal() as session:
            default_cluster = ensure_default_cluster(session)
            clusters = list_active_clusters(session)
            session.expunge_all()
        _cluster_cache = (
            owner,
            now + _CLUSTER_CACHE_TTL_SECONDS,
            tuple(clusters),
            default_cluster.id,
        )
        return list(clusters), default_cluster


def resolve_cluster_selection(
    requested_cluster_id: str, session_cluster_id: str = ""
) -> tuple[list[Cluster], Cluster]:
    """Resolve active clusters without depending on any dashboard route."""
    clusters, default_cluster = _active_clusters()
    by_id = {cluster.id: cluster for cluster in clusters}
    selected = by_id.get(requested_cluster_id) if requested_cluster_id else None
    if selected is None and session_cluster_id:
        selected = by_id.get(session_cluster_id)
    return clusters, (selected or default_cluster)


def selected_cluster(request: Request) -> Cluster:
    """Return the active cluster selected by ``?cluster=``/``?cluster_id=`` or the session."""
    _clusters, cluster = resolve_cluster_selection(
        (
            request.query_params.get("cluster_id", "").strip()
            or request.query_params.get("cluster", "").strip()
        ),
        request.session.get("selected_cluster_id", ""),
    )
    request.session["selected_cluster_id"] = cluster.id
    return cluster


def cluster_selection(request: Request) -> tuple[list[Cluster], Cluster]:
    """Return switcher choices and persist the selected cluster."""
    clusters, cluster = resolve_cluster_selection(
        (
            request.query_params.get("cluster_id", "").strip()
            or request.query_params.get("cluster", "").strip()
        ),
        request.session.get("selected_cluster_id", ""),
    )
    request.session["selected_cluster_id"] = cluster.id
    return clusters, cluster


def cluster_connection(cluster: Cluster) -> tuple[list[str], str, str, str, str]:
    """Return arguments accepted by ``run_ceph_json_command_with``."""
    nodes = [node.strip() for node in cluster.ceph_mon_nodes.split(",") if node.strip()]
    ssh_user, ssh_key_path, exec_mode, container_name = resolve_ssh_creds(cluster)
    return nodes, container_name, ssh_user, ssh_key_path, exec_mode


def require_default_cluster(request: Request, feature_name: str) -> Cluster:
    """Fail closed when a legacy feature would silently use `.env` scope."""
    cluster = selected_cluster(request)
    if not cluster.is_default:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{feature_name} hiện chỉ hỗ trợ cụm mặc định. "
                f"Bạn đang chọn cụm {cluster.name!r}; hãy chuyển về cụm mặc định trước khi tiếp tục."
            ),
        )
    return cluster

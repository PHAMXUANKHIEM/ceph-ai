"""Resolve the Ceph connection selected by the current dashboard session."""

from threading import Lock
from time import monotonic

from fastapi import HTTPException, Request
from sqlalchemy import event
from sqlalchemy.orm import Session

from shared import db
from shared.clusters import ensure_default_cluster, list_active_clusters
from shared.cluster_nodes import resolve_ssh_creds
from shared.models import Cluster
from dashboard.cluster_authorization import authorized_cluster_ids


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


@event.listens_for(Session, "after_flush")
def _invalidate_on_cluster_write(session, _flush_context) -> None:
    """Any Cluster insert/update/delete in this process drops the cache.

    Routes call clear_cluster_selection_cache() explicitly, but a write from
    anywhere else (CLI, background job, tests) would otherwise leave a
    just-added cluster invisible for up to the TTL, and selection would fall
    back to the default cluster instead of the one requested.
    """
    if any(isinstance(item, Cluster) for item in (*session.new, *session.dirty, *session.deleted)):
        session.info["cluster_selection_dirty"] = True
        clear_cluster_selection_cache()


@event.listens_for(Session, "after_commit")
def _invalidate_after_cluster_commit(session) -> None:
    # A request between flush and commit may have re-cached the pre-commit
    # rows from its own session; drop that too once the write is visible.
    if session.info.pop("cluster_selection_dirty", False):
        clear_cluster_selection_cache()


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
    clusters, cluster = resolve_cluster_selection(
        (
            request.query_params.get("cluster_id", "").strip()
            or request.query_params.get("cluster", "").strip()
        ),
        request.session.get("selected_cluster_id", ""),
    )
    cluster = _enforce_request_cluster_access(request, clusters, cluster)
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
    cluster = _enforce_request_cluster_access(request, clusters, cluster)
    allowed_ids = authorized_cluster_ids(str(request.session.get("user") or ""))
    if allowed_ids is not None:
        clusters = [
            item for item in clusters
            if item.id in allowed_ids or (not allowed_ids and item.is_default)
        ]
    request.session["selected_cluster_id"] = cluster.id
    return clusters, cluster


def _enforce_request_cluster_access(
    request: Request, clusters: list[Cluster], selected: Cluster
) -> Cluster:
    """Apply the user/cluster grant at the common selection boundary."""
    actor = str(request.session.get("user") or "")
    allowed_ids = authorized_cluster_ids(actor)
    if allowed_ids is None:
        return selected
    requested = (
        request.query_params.get("cluster_id", "").strip()
        or request.query_params.get("cluster", "").strip()
    )
    if requested and requested not in allowed_ids and not (not allowed_ids and selected.is_default):
        raise HTTPException(status_code=403, detail="Bạn không được truy cập cụm đã chọn")
    if selected.id in allowed_ids or (not allowed_ids and selected.is_default):
        return selected
    for cluster in clusters:
        if cluster.id in allowed_ids:
            return cluster
    raise HTTPException(status_code=403, detail="Tài khoản chưa được cấp quyền trên cluster nào")


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

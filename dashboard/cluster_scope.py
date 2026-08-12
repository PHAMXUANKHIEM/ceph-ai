"""Resolve the Ceph connection selected by the current dashboard session."""

from fastapi import Request

from dashboard.routes.incidents import _resolve_selected_cluster
from shared.cluster_nodes import resolve_ssh_creds
from shared.models import Cluster


def selected_cluster(request: Request) -> Cluster:
    """Return the active cluster selected by ``?cluster=`` or the session."""
    _clusters, cluster = _resolve_selected_cluster(
        request.query_params.get("cluster", "").strip(),
        request.session.get("selected_cluster_id", ""),
    )
    request.session["selected_cluster_id"] = cluster.id
    return cluster


def cluster_selection(request: Request) -> tuple[list[Cluster], Cluster]:
    """Return switcher choices and persist the selected cluster."""
    clusters, cluster = _resolve_selected_cluster(
        request.query_params.get("cluster", "").strip(),
        request.session.get("selected_cluster_id", ""),
    )
    request.session["selected_cluster_id"] = cluster.id
    return clusters, cluster


def cluster_connection(cluster: Cluster) -> tuple[list[str], str, str, str, str]:
    """Return arguments accepted by ``run_ceph_json_command_with``."""
    nodes = [node.strip() for node in cluster.ceph_mon_nodes.split(",") if node.strip()]
    ssh_user, ssh_key_path, exec_mode, container_name = resolve_ssh_creds(cluster)
    return nodes, container_name, ssh_user, ssh_key_path, exec_mode

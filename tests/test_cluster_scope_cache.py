from shared import db
from shared.models import Cluster

from dashboard import cluster_scope


def test_cluster_written_outside_routes_is_visible_to_selection_immediately(dashboard_client):
    clusters, default = cluster_scope.resolve_cluster_selection("")
    with db.SessionLocal() as session:
        added = Cluster(
            name="cache-probe", ceph_mon_nodes="10.0.0.9", ssh_user="root",
            ssh_key_path="/tmp/none", is_default=False, is_active=True,
        )
        session.add(added)
        session.commit()
        added_id = added.id

    # Within the cache TTL: the new cluster must be selectable, not replaced
    # by the default cluster.
    _clusters, selected = cluster_scope.resolve_cluster_selection(added_id)
    assert selected.id == added_id
    assert selected.id != default.id

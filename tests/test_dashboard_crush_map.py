import json
from datetime import datetime, timedelta

import bcrypt

from shared import db as db_module
from shared.models import Cluster, CrushOsdDistribution, CrushStructureSnapshot, User


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def _login_as_non_admin(client, username="regular", password="s3cret-pw"):
    with db_module.SessionLocal() as session:
        session.add(
            User(
                username=username,
                password_hash=bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode(),
                is_admin=False,
                is_active=True,
                created_by="admin",
            )
        )
        session.commit()
    client.post("/login", data={"username": username, "password": password})


def _sample_tree(host_weight=None, osd_weight=65536, second_osd=True):
    children = [{"id": 0, "name": "osd.0", "type": "osd", "weight": osd_weight, "children": []}]
    if second_osd:
        children.append({"id": 1, "name": "osd.1", "type": "osd", "weight": osd_weight, "children": []})
    return {
        "roots": [
            {
                "id": -1,
                "name": "default",
                "type": "root",
                "weight": None,
                "children": [
                    {"id": -2, "name": "host1", "type": "host", "weight": host_weight, "children": children},
                ],
            }
        ]
    }


def _empty_tree():
    return {"roots": [{"id": -1, "name": "default", "type": "root", "weight": None, "children": []}]}


def _add_snapshot(tree, diff=None, created_at=None, cluster_id=None):
    with db_module.SessionLocal() as session:
        cluster_id = cluster_id or session.query(Cluster.id).filter(Cluster.is_default.is_(True)).scalar()
        row = CrushStructureSnapshot(
            cluster_id=cluster_id,
            tree_json=json.dumps(tree),
            diff_json=json.dumps(diff) if diff is not None else None,
        )
        if created_at is not None:
            row.created_at = created_at
        session.add(row)
        session.commit()
        return row.id


def _add_distribution(osd_id, bytes_used, bytes_total, pgs, host="host1", cluster_id=None):
    with db_module.SessionLocal() as session:
        cluster_id = cluster_id or session.query(Cluster.id).filter(Cluster.is_default.is_(True)).scalar()
        session.add(
            CrushOsdDistribution(
                cluster_id=cluster_id, osd_id=osd_id, host=host,
                bytes_used=bytes_used, bytes_total=bytes_total, pgs=pgs
            )
        )
        session.commit()


# --- Page + auth gating -----------------------------------------------


def test_unauthenticated_get_crush_map_redirects_to_login(dashboard_client):
    response = dashboard_client.get("/crush-map", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_get_crush_map_page_for_admin(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/crush-map")
    assert response.status_code == 200
    assert "CRUSH Map" in response.text


def test_get_crush_map_page_rejects_non_admin(dashboard_client):
    _login_as_non_admin(dashboard_client)
    response = dashboard_client.get("/crush-map")
    assert response.status_code == 403


def test_nav_shows_crush_map_link_for_admin_on_other_pages(dashboard_client):
    # "/" (dashboard home) deliberately excluded here: it currently 500s in
    # this working tree due to a pre-existing, unrelated in-progress
    # multi-cluster migration (shared/heartbeat.py::get_latest() gained a
    # required cluster_id param that dashboard/routes/incidents.py's call
    # site was never updated for) — not something this story touches or
    # should fix, see Dev Notes/git status disclosure.
    _login(dashboard_client)
    for path in ("/nodes", "/upgrade", "/settings"):
        response = dashboard_client.get(path)
        assert 'href="/crush-map"' in response.text, f"missing CRUSH Map nav link on {path}"


def test_api_endpoints_reject_non_admin(dashboard_client):
    _login_as_non_admin(dashboard_client)
    assert dashboard_client.get("/api/crush-map/tree").status_code == 403
    assert dashboard_client.get("/api/crush-map/history").status_code == 403
    assert dashboard_client.get("/api/crush-map/history/some-id").status_code == 403


# --- GET /api/crush-map/tree — 3-state contract -------------------------


def test_api_tree_no_snapshot_yet(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/api/crush-map/tree")
    assert response.status_code == 200
    assert response.json() == {"state": "no_snapshot_yet"}


def test_second_cluster_page_and_tree_are_fully_scoped(dashboard_client):
    _login(dashboard_client)
    with db_module.SessionLocal() as session:
        second = Cluster(
            name="cluster-2", ceph_mon_nodes="10.0.0.2",
            ssh_user="ceph", ssh_key_path="/tmp/test-key", is_active=True,
        )
        session.add(second)
        session.commit()
        second_id = second.id

    _add_snapshot(_sample_tree(second_osd=False), cluster_id=second_id)
    _add_distribution(
        0, bytes_used=777, bytes_total=1000, pgs=17,
        host="cluster-2-host", cluster_id=second_id,
    )

    page = dashboard_client.get("/crush-map", params={"cluster": second_id})
    assert page.status_code == 200
    assert "cluster-2" in page.text
    assert f'value="{second_id}" selected' in page.text

    data = dashboard_client.get("/api/crush-map/tree").json()
    osd = data["roots"][0]["children"][0]["children"][0]
    assert osd["bytes_used"] == 777
    assert osd["host"] == "cluster-2-host"


def test_second_cluster_cannot_read_default_cluster_history_detail(dashboard_client):
    _login(dashboard_client)
    default_snapshot_id = _add_snapshot(
        _sample_tree(), diff={"added": [], "removed": [], "reweighted": []}
    )
    with db_module.SessionLocal() as session:
        second = Cluster(
            name="cluster-2", ceph_mon_nodes="10.0.0.2",
            ssh_user="ceph", ssh_key_path="/tmp/test-key", is_active=True,
        )
        session.add(second)
        session.commit()
        second_id = second.id

    dashboard_client.get("/crush-map", params={"cluster": second_id})
    response = dashboard_client.get(f"/api/crush-map/history/{default_snapshot_id}")
    assert response.status_code == 404


def test_api_tree_empty_cluster(dashboard_client):
    _login(dashboard_client)
    _add_snapshot(_empty_tree())

    response = dashboard_client.get("/api/crush-map/tree")

    assert response.status_code == 200
    data = response.json()
    assert data["state"] == "empty_cluster"
    assert "snapshot_id" in data and "created_at" in data


def test_api_tree_ok_aggregates_host_from_osd_distribution(dashboard_client):
    _login(dashboard_client)
    _add_snapshot(_sample_tree())
    _add_distribution(0, bytes_used=100, bytes_total=200, pgs=10)
    _add_distribution(1, bytes_used=300, bytes_total=400, pgs=20)

    response = dashboard_client.get("/api/crush-map/tree")

    assert response.status_code == 200
    data = response.json()
    assert data["state"] == "ok"
    host = data["roots"][0]["children"][0]
    assert host["type"] == "host"
    assert host["has_distribution_data"] is True
    assert host["partial_distribution_data"] is False
    # AD-27: sum(bytes_used)/sum(bytes_total), never an average of percentages
    assert host["bytes_used"] == 400
    assert host["bytes_total"] == 600
    assert host["pgs"] == 30

    osd0 = host["children"][0]
    assert osd0["bytes_used"] == 100
    assert osd0["bytes_total"] == 200
    assert osd0["weight_normalized"] == 1.0


def test_api_tree_ok_partial_distribution_data(dashboard_client):
    _login(dashboard_client)
    _add_snapshot(_sample_tree())
    _add_distribution(0, bytes_used=100, bytes_total=200, pgs=10)
    # osd.1 never scanned — no CrushOsdDistribution row for id 1

    response = dashboard_client.get("/api/crush-map/tree")

    data = response.json()
    host = data["roots"][0]["children"][0]
    assert host["has_distribution_data"] is True
    assert host["partial_distribution_data"] is True
    assert host["bytes_used"] == 100  # only osd.0 contributed, not treated as osd.1=0
    osd1 = host["children"][1]
    assert osd1["has_distribution_data"] is False
    assert osd1["bytes_used"] is None


def test_api_tree_returns_crush_rules_from_snapshot(dashboard_client):
    _login(dashboard_client)
    tree = _sample_tree()
    tree["rules"] = [{"rule_id": 2, "rule_name": "ssd_replicated", "type": 1, "min_size": 1, "max_size": 10,
                     "steps": [{"op": "take", "item": -1, "item_name": "default"}, {"op": "emit"}]}]
    _add_snapshot(tree)
    response = dashboard_client.get("/api/crush-map/tree")
    assert response.status_code == 200
    assert response.json()["rules"] == tree["rules"]


def test_api_tree_old_snapshot_without_rules_returns_empty_list(dashboard_client):
    _login(dashboard_client)
    _add_snapshot(_sample_tree())
    response = dashboard_client.get("/api/crush-map/tree")
    assert response.status_code == 200
    assert response.json()["rules"] == []


def test_api_tree_ok_no_distribution_data_at_all(dashboard_client):
    _login(dashboard_client)
    _add_snapshot(_sample_tree())

    response = dashboard_client.get("/api/crush-map/tree")

    data = response.json()
    host = data["roots"][0]["children"][0]
    assert host["has_distribution_data"] is False
    assert host["partial_distribution_data"] is False
    osd0 = host["children"][0]
    assert osd0["has_distribution_data"] is False


def test_api_tree_tolerates_osd_leaf_with_null_weight(dashboard_client):
    """Pre-Story-12.2-fix snapshots have weight=None on every OSD leaf
    (crush_structure_monitor.py's own disclosed gap) — must render as
    'no Weight data', not crash."""
    _login(dashboard_client)
    _add_snapshot(_sample_tree(osd_weight=None))

    response = dashboard_client.get("/api/crush-map/tree")

    assert response.status_code == 200
    osd0 = response.json()["roots"][0]["children"][0]["children"][0]
    assert osd0["weight_normalized"] is None


def test_api_tree_marks_added_and_reweighted_nodes_from_latest_diff(dashboard_client):
    _login(dashboard_client)
    diff = {
        "added": [{"id": 1, "name": "osd.1", "type": "osd", "weight": 65536}],
        "removed": [],
        "reweighted": [{"id": 0, "name": "osd.0", "type": "osd", "old_weight": 65536, "new_weight": 32768}],
    }
    _add_snapshot(_sample_tree(), diff=diff)

    response = dashboard_client.get("/api/crush-map/tree")

    host = response.json()["roots"][0]["children"][0]
    osd0, osd1 = host["children"][0], host["children"][1]
    assert osd0["recent_change"]["kind"] == "reweighted"
    assert osd0["recent_change"]["old_weight"] == 65536
    assert osd1["recent_change"]["kind"] == "added"
    # A node not present in the diff at all is not flagged.
    assert host["recent_change"] is None


def test_api_tree_first_snapshot_has_no_recent_change_markers(dashboard_client):
    """diff_json is None on the very first snapshot ever taken (no baseline
    to diff against) — must not be treated as an error."""
    _login(dashboard_client)
    _add_snapshot(_sample_tree(), diff=None)

    response = dashboard_client.get("/api/crush-map/tree")

    host = response.json()["roots"][0]["children"][0]
    assert host["children"][0]["recent_change"] is None


def test_api_tree_recent_change_expires_after_cutoff(dashboard_client):
    _login(dashboard_client)
    diff = {"added": [{"id": 1, "name": "osd.1", "type": "osd", "weight": 65536}], "removed": [], "reweighted": []}
    _add_snapshot(_sample_tree(), diff=diff, created_at=datetime.utcnow() - timedelta(hours=25))

    response = dashboard_client.get("/api/crush-map/tree")

    host = response.json()["roots"][0]["children"][0]
    # Still the latest (and only) snapshot, but old enough that the "recent
    # change" marker must no longer show (FR-4's auto-hide requirement).
    assert host["children"][1]["recent_change"] is None


# --- GET /api/crush-map/history -----------------------------------------


def test_api_history_excludes_first_snapshot_without_diff(dashboard_client):
    _login(dashboard_client)
    _add_snapshot(_sample_tree(), diff=None)  # first snapshot, no diff

    response = dashboard_client.get("/api/crush-map/history")

    assert response.status_code == 200
    assert response.json()["items"] == []


def test_api_history_lists_newest_first_with_summary_counts(dashboard_client):
    _login(dashboard_client)
    _add_snapshot(_sample_tree(), diff=None, created_at=datetime.utcnow() - timedelta(hours=3))
    diff1 = {"added": [{"id": 1, "name": "osd.1", "type": "osd", "weight": 65536}], "removed": [], "reweighted": []}
    _add_snapshot(_sample_tree(), diff=diff1, created_at=datetime.utcnow() - timedelta(hours=2))
    diff2 = {"added": [], "removed": [{"id": 2, "name": "osd.2", "type": "osd", "weight": 65536}], "reweighted": []}
    _add_snapshot(_sample_tree(), diff=diff2, created_at=datetime.utcnow() - timedelta(hours=1))

    response = dashboard_client.get("/api/crush-map/history")

    items = response.json()["items"]
    assert len(items) == 2
    assert items[0]["removed_count"] == 1  # newest first
    assert items[1]["added_count"] == 1


def test_api_history_pagination_with_before_cursor(dashboard_client):
    _login(dashboard_client)
    for i in range(3):
        diff = {"added": [{"id": i, "name": f"osd.{i}", "type": "osd", "weight": 65536}], "removed": [], "reweighted": []}
        _add_snapshot(_sample_tree(), diff=diff, created_at=datetime.utcnow() - timedelta(hours=3 - i))

    first_page = dashboard_client.get("/api/crush-map/history?limit=2").json()
    assert len(first_page["items"]) == 2
    assert first_page["next_before"] is not None

    second_page = dashboard_client.get(
        "/api/crush-map/history", params={"limit": 2, "before": first_page["next_before"]}
    ).json()
    assert len(second_page["items"]) == 1
    assert second_page["next_before"] is None

    first_ids = {item["id"] for item in first_page["items"]}
    second_ids = {item["id"] for item in second_page["items"]}
    assert not (first_ids & second_ids)


def test_api_history_detail_by_id(dashboard_client):
    _login(dashboard_client)
    diff = {
        "added": [{"id": 1, "name": "osd.1", "type": "osd", "weight": 65536}],
        "removed": [{"id": 2, "name": "osd.2", "type": "osd", "weight": 65536}],
        "reweighted": [{"id": 0, "name": "osd.0", "type": "osd", "old_weight": 65536, "new_weight": 32768}],
    }
    snapshot_id = _add_snapshot(_sample_tree(), diff=diff)

    response = dashboard_client.get(f"/api/crush-map/history/{snapshot_id}")

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == snapshot_id
    assert len(data["added"]) == 1
    assert len(data["removed"]) == 1
    assert len(data["reweighted"]) == 1


def test_api_history_detail_404_for_unknown_id(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/api/crush-map/history/does-not-exist")
    assert response.status_code == 404

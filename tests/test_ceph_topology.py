import copy
from types import SimpleNamespace

from shared import ceph_topology

CLUSTER = SimpleNamespace(id="c1", name="LAB")


def _osd(osd_id, host):
    return {"id": osd_id, "name": f"osd.{osd_id}", "type": "osd", "host": host}


def _addrs(ip, *ports):
    return [{"type": kind, "addr": f"{ip}:{port}"} for kind, port in zip(("v2", "v1"), ports)]


def _daemons(*, down=(), out=(), cluster_ip=lambda n: f"198.51.100.1{n}"):
    hosts = {0: 1, 3: 1, 1: 2, 4: 2, 2: 3, 5: 3}
    return {
        "mons": [{"name": f"node{n}", "rank": n - 1, "in_quorum": True,
                  "public": _addrs(f"198.51.100.1{n}", 3300, 6789)} for n in (1, 2, 3)],
        "osds": [{"id": osd, "up": osd not in down, "in": osd not in out,
                  "public": _addrs(f"198.51.100.1{host}", 6800 + 4 * (osd // 3), 6801 + 4 * (osd // 3)),
                  "cluster": _addrs(cluster_ip(host), 6802 + 4 * (osd // 3), 6803 + 4 * (osd // 3))}
                 for osd, host in sorted(hosts.items())],
    }


def _sections(*, checks=None, stale=False, cluster_network=None, quorum=3, osd_up=6, pg_clean=100,
              pool_apps=(("rbd",), ("rgw",)), daemons=None):
    hosts = [("node1", ["192.0.2.11", "198.51.100.11"]), ("node2", ["192.0.2.12", "198.51.100.12"]),
             ("node3", ["192.0.2.13", "198.51.100.13"])]
    status = {
        "health": {"status": "HEALTH_OK", "checks": {}},
        "quorum_names": [f"node{i}" for i in range(1, quorum + 1)],
        "monmap": {"epoch": 7, "num_mons": 3, "min_mon_release_name": "tentacle"},
        "osdmap": {"epoch": 99, "num_osds": 6, "num_up_osds": osd_up, "num_in_osds": 6},
        "pgmap": {"pgs_by_state": [{"state_name": "active+clean", "count": pg_clean}]
                  + ([{"state_name": "active+undersized+degraded", "count": 100 - pg_clean}] if pg_clean < 100 else []),
                  "num_pgs": 100, "num_pools": 3, "num_objects": 10, "bytes_used": 50, "bytes_total": 200},
        "mgrmap": {"available": True, "num_standbys": 1, "modules": ["prometheus"], "services": {}},
        "fsmap": {"epoch": 1, "by_rank": []},
        "servicemap": {"services": {"rgw": {"daemons": {"summary": "", "4242": {
            "metadata": {"hostname": "node1.example", "frontend_config#0": "beast port=8000", "zonegroup_name": "zg"}}}}}},
    }
    return {
        "health": {"stale": stale, "collected_at": "2026-10-06T03:00:00Z",
                   "health": {"status": "HEALTH_WARN" if checks else "HEALTH_OK", "checks": checks or {}}},
        "status": {"stale": stale, "collected_at": "2026-10-06T03:00:00Z", "status": status},
        "nodes": {"collected_at": "2026-10-06T03:00:00Z", "nodes": {
            "nodes": [{"host": addrs[0], "node_name": name, "roles": ["MON", "OSD"], "aliases": addrs} for name, addrs in hosts],
            "networks": {"public": ["198.51.100.0/24"], "cluster": cluster_network or []},
            **({"daemons": daemons} if daemons is not None else {}),
        }},
        "crush": {"crush": {"roots": [{"type": "root", "name": "default", "children": [
            {"type": "host", "name": "node1~hdd", "children": [_osd(0, "node1"), _osd(3, "node1")]},
            {"type": "host", "name": "node2~hdd", "children": [_osd(1, "node2"), _osd(4, "node2")]},
            {"type": "host", "name": "node3~hdd", "children": [_osd(2, "node3"), _osd(5, "node3")]},
        ]}]}},
        "pools": {"pools": [{"name": f"p{i}", "applications": list(apps)} for i, apps in enumerate(pool_apps)]},
    }


def _node(topology, node_id):
    return next(node for node in topology["nodes"] if node["id"] == node_id)


def test_healthy_cluster_has_every_service_host_and_network():
    topology = ceph_topology.build(CLUSTER, sections=_sections())

    ids = {node["id"] for node in topology["nodes"]}
    assert {"mon", "mgr", "osd", "pools", "rgw", "ceph_ai", "rbd_clients", "s3_clients",
            "net_public", "net_management", "host_node1", "host_node2", "host_node3"} <= ids
    assert "mds" not in ids and "net_cluster" not in ids
    assert {node["status"] for node in topology["nodes"]} == {"ok"}
    assert _node(topology, "host_node1")["facts"][0] == "OSD: osd.0, osd.3"
    assert "Không có cluster_network" in " ".join(_node(topology, "net_public")["facts"])
    assert topology["summary"]["used_percent"] == 25.0
    node_ids = ids
    assert all({edge["from"], edge["to"]} <= node_ids for edge in topology["edges"])


def test_down_osd_marks_the_osd_service_and_only_its_host():
    checks = {"OSD_DOWN": {"severity": "HEALTH_WARN", "summary": {"message": "1 osds down"},
                           "detail": [{"message": "osd.4 (root=default,host=node2) is down"}]}}

    topology = ceph_topology.build(CLUSTER, sections=_sections(checks=checks, osd_up=5))

    assert _node(topology, "osd")["status"] == "error"
    assert _node(topology, "host_node2")["status"] == "error"
    assert "osd.4 (down)" in _node(topology, "host_node2")["facts"][0]
    assert _node(topology, "host_node1")["status"] == "ok"


def test_degraded_pgs_and_lost_mon_quorum():
    checks = {"PG_DEGRADED": {"severity": "HEALTH_WARN", "summary": {"message": "degraded"}},
              "MON_DOWN": {"severity": "HEALTH_WARN", "summary": {"message": "1/3 mons down"},
                           "detail": [{"message": "mon.node3 (rank 2) addr is down (out of quorum)"}]}}

    topology = ceph_topology.build(CLUSTER, sections=_sections(checks=checks, quorum=2, pg_clean=80))

    assert _node(topology, "pools")["status"] == "warn"
    assert _node(topology, "mon")["status"] == "error"
    assert _node(topology, "host_node3")["status"] == "warn"


def test_slow_heartbeats_mark_the_network_they_travel_on():
    checks = {"OSD_SLOW_PING_TIME_BACK": {"severity": "HEALTH_WARN", "summary": {"message": "slow back"}}}

    shared_network = ceph_topology.build(CLUSTER, sections=_sections(checks=checks))
    split_network = ceph_topology.build(CLUSTER, sections=_sections(checks=checks, cluster_network=["192.0.2.0/24"]))

    assert _node(shared_network, "net_public")["status"] == "warn"
    assert _node(split_network, "net_cluster")["status"] == "warn"
    assert _node(split_network, "net_public")["status"] == "ok"
    assert "net_management" not in {node["id"] for node in split_network["nodes"]}


def test_daemon_addresses_show_on_hosts_mon_and_networks():
    topology = ceph_topology.build(CLUSTER, sections=_sections(daemons=_daemons(out=(5,))))

    host_facts = _node(topology, "host_node2")["facts"]
    assert "mon.node2: v2 198.51.100.12:3300 · v1 198.51.100.12:6789" in host_facts
    assert "osd.1: public v2 198.51.100.12:6800 · v1 198.51.100.12:6801 | cluster v2 198.51.100.12:6802 · v1 198.51.100.12:6803" in host_facts
    assert "mon.node1: v2 198.51.100.11:3300 · v1 198.51.100.11:6789" in _node(topology, "mon")["facts"]
    assert "OSD out: osd.5" in _node(topology, "osd")["facts"]
    public = _node(topology, "net_public")
    assert public["status"] == "ok" and "Daemon lắng nghe: MON ×3, OSD ×6" in public["facts"]
    labels = {(edge["from"], edge["to"]): edge["label"] for edge in topology["edges"]}
    assert labels[("mon", "net_public")] == "lắng nghe :3300, :6789"
    assert labels[("osd", "net_public")] == "client I/O · replication"


def test_osd_dump_names_down_osds_missing_from_a_truncated_health_detail():
    checks = {"OSD_DOWN": {"severity": "HEALTH_WARN", "summary": {"message": "1 osds down"}}}

    topology = ceph_topology.build(CLUSTER, sections=_sections(checks=checks, osd_up=5, daemons=_daemons(down=(4,))))

    assert "osd.4 (down)" in _node(topology, "host_node2")["facts"][0]
    assert _node(topology, "host_node2")["status"] == "error"
    assert _node(topology, "host_node1")["status"] == "ok"


def test_an_old_osd_dump_does_not_override_fresh_health():
    # The inventory still lists osd.4 down but health says every OSD is up again.
    topology = ceph_topology.build(CLUSTER, sections=_sections(daemons=_daemons(down=(4,))))

    assert _node(topology, "host_node2")["status"] == "ok"
    assert "(down)" not in _node(topology, "host_node2")["facts"][0]


def test_cluster_addresses_outside_cluster_network_are_flagged():
    daemons = _daemons(cluster_ip=lambda n: "192.0.2.11" if n == 1 else f"198.51.100.1{n}")

    topology = ceph_topology.build(CLUSTER, sections=_sections(cluster_network=["192.0.2.0/24"], daemons=daemons))

    cluster = _node(topology, "net_cluster")
    assert cluster["status"] == "warn"
    outside = next(fact for fact in cluster["facts"] if fact.startswith("Địa chỉ cluster ngoài"))
    assert "198.51.100.12:6802" in outside and "192.0.2.11" not in outside


def test_slow_ping_details_list_the_osd_pairs():
    detail = [{"message": f"Slow OSD heartbeats on back from osd.{i} to osd.{i + 1} 1200 msec"} for i in range(7)]
    checks = {"OSD_SLOW_PING_TIME_BACK": {"severity": "HEALTH_WARN", "summary": {"message": "slow back"},
                                          "detail": detail}}

    facts = _node(ceph_topology.build(CLUSTER, sections=_sections(checks=checks)), "net_public")["facts"]

    assert "Heartbeat chậm: Slow OSD heartbeats on back from osd.0 to osd.1 1200 msec" in facts
    assert "… và 2 cặp OSD khác" in facts


def test_muted_checks_do_not_colour_the_graph():
    checks = {"AUTH_INSECURE_KEYS_ALLOWED": {"severity": "HEALTH_WARN", "muted": True,
                                             "summary": {"message": "insecure keys"}}}

    topology = ceph_topology.build(CLUSTER, sections=_sections(checks=checks))

    assert _node(topology, "mon")["status"] == "ok"


def test_stale_or_missing_snapshots_are_unknown_not_healthy():
    stale = ceph_topology.build(CLUSTER, sections=_sections(stale=True))
    empty = ceph_topology.build(CLUSTER, sections={"health": None, "status": None, "nodes": None,
                                                   "crush": None, "pools": None})

    assert stale["stale"] and {node["status"] for node in stale["nodes"]} == {"unknown"}
    assert empty["stale"] and {node["status"] for node in empty["nodes"]} == {"unknown"}


def test_clients_follow_pool_applications():
    topology = ceph_topology.build(CLUSTER, sections=_sections(pool_apps=(("cephfs",),)))

    ids = {node["id"] for node in topology["nodes"]}
    assert "cephfs_clients" in ids and "mds" in ids
    assert "rbd_clients" not in ids and "s3_clients" not in ids


def test_topology_carries_no_secrets():
    sections = _sections()
    sections_copy = copy.deepcopy(sections)
    sections_copy["status"]["status"]["mgrmap"]["services"] = {"dashboard": "https://198.51.100.11:8443/"}

    text = str(ceph_topology.build(CLUSTER, sections=sections_copy))

    for forbidden in ("key", "token", "password", "secret"):
        assert forbidden not in text.lower()


def test_api_and_page_are_admin_only(dashboard_client, monkeypatch):
    from dashboard.routes import installation_stream

    monkeypatch.setattr(installation_stream.ceph_topology, "build", lambda cluster: {"schema": ceph_topology.SCHEMA, "nodes": []})
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})

    assert dashboard_client.get("/api/stream/ceph-topology").json()["schema"] == ceph_topology.SCHEMA
    page = dashboard_client.get("/stream")
    assert page.status_code == 200 and 'id="ceph-topology-bootstrap"' in page.text

    monkeypatch.setattr(installation_stream.auth, "is_admin_user", lambda _user: False)
    assert dashboard_client.get("/api/stream/ceph-topology").status_code == 403

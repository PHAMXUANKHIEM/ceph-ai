from watcher.block_storage_policy import build_durability_policy


def _tree(hosts):
    nodes = [{"id": -1, "type": "root", "name": "default", "children": [-(index + 2) for index in range(len(hosts))]}]
    for index, host in enumerate(hosts):
        host_id = -(index + 2)
        osd_id = index
        nodes.append({"id": host_id, "type": "host", "name": host, "children": [osd_id]})
        nodes.append({"id": osd_id, "type": "osd", "status": "up"})
    return {"nodes": nodes}


def _rules(rule_id=0, domain="host"):
    return {"rules": [{
        "rule_id": rule_id, "rule_name": "replicated_rule",
        "steps": [{"op": "take", "item": -1}, {"op": "chooseleaf_firstn", "num": 0, "type": domain}, {"op": "emit"}],
    }]}


def test_replicated_policy_matches_available_failure_domains():
    result = build_durability_policy(
        {"type": "replicated", "replica_size": 3, "min_size": 2, "crush_rule": 0},
        _rules(), _tree(["node-a", "node-b", "node-c"]),
    )

    assert result["status"] == "HEALTHY"
    assert result["policy"] == {"mode": "replicated", "size": 3, "min_size": 2}
    assert result["crush"]["available_domains"] == 3
    assert result["crush"]["required_domains"] == 3
    assert result["read_only"] is True


def test_replicated_policy_flags_insufficient_failure_domains():
    result = build_durability_policy(
        {"type": "replicated", "replica_size": 3, "min_size": 2, "crush_rule": 0},
        _rules(), _tree(["node-a", "node-b"]),
    )

    assert result["status"] == "CRITICAL"
    assert "Chỉ có 2 failure domain" in result["notes"][0]


def test_ec_policy_requires_k_plus_m_failure_domains():
    result = build_durability_policy(
        {"type": "erasure", "erasure_code_profile": "ec42", "crush_rule": 0},
        _rules(), _tree(["node-a", "node-b", "node-c", "node-d", "node-e"]),
        ec_profile={"k": "4", "m": "2"},
    )

    assert result["status"] == "CRITICAL"
    assert result["policy"]["chunks"] == 6
    assert result["crush"]["required_domains"] == 6


def test_policy_fails_closed_when_rule_or_topology_is_missing():
    result = build_durability_policy(
        {"type": "replicated", "replica_size": 3, "min_size": 2, "crush_rule": 0},
        {"rules": []}, {},
    )

    assert result["status"] == "INSUFFICIENT_EVIDENCE"
    assert result["read_only"] is True
    assert result["evidence"]["gaps"]

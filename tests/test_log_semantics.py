from watcher.log_semantics import derive_identity, same_semantic_problem


def test_derive_identity_is_server_owned_and_normalized():
    identity = derive_identity(
        ["osd.<ID> heartbeat_check: no reply from <ADDR>"],
        ["NODE-A"],
        ["osd.5"],
    )
    assert identity.fault_family == "network_heartbeat"
    assert identity.entities == ("daemon:osd.5", "host:node-a")


def test_derive_identity_extracts_volume_from_rbd_performance_log():
    identity = derive_identity(
        ["RBD image vms/Disk-01 latency is high and IOPS throttled"], [], []
    )
    assert identity.fault_family == "volume_saturation"
    assert identity.entities == ("volume:vms/disk-01",)


def test_derive_identity_extracts_separate_pool_and_image_fields():
    identity = derive_identity(
        ["volume slow: pool=vms image=database-2 latency 44ms"], [], []
    )
    assert identity.fault_family == "volume_saturation"
    assert "volume:vms/database-2" in identity.entities


def test_derive_identity_marks_node_resource_pressure_with_server_host():
    identity = derive_identity(
        ["CPU pressure remains high and node is overloaded"], ["NODE-A"], []
    )
    assert identity.fault_family == "node_resource"
    assert identity.entities == ("host:node-a",)


def test_unknown_family_fails_closed():
    identity = derive_identity(["an unfamiliar message"], ["node-a"], ["osd.5"])
    assert identity.fault_family is None
    assert not same_semantic_problem(None, set(identity.entities), None, set(identity.entities))


def test_rgw_vault_key_failures_have_a_stable_server_family():
    identity = derive_identity(
        ["req <N> ERROR: Vault token file '<PATH>' not found",
         "ERROR: failed to retrieve actual key from key_id: <UUID>"],
        ["rgw-1"], ["rgw"],
    )
    assert identity.fault_family == "rgw_encryption_key"


def test_generic_daemon_fallback_requires_an_explicit_failure_hint():
    assert derive_identity(["manager module failed to load"], [], ["mgr"]).fault_family == "mgr_operational"
    assert derive_identity(["manager module loaded"], [], ["mgr"]).fault_family is None

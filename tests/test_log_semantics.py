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


def test_unknown_family_fails_closed():
    identity = derive_identity(["an unfamiliar message"], ["node-a"], ["osd.5"])
    assert identity.fault_family is None
    assert not same_semantic_problem(None, set(identity.entities), None, set(identity.entities))

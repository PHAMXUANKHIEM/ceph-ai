from watcher.log_semantics import derive_identity, same_semantic_problem


def test_derive_identity_is_server_owned_and_normalized():
    identity = derive_identity(
        ["osd.<ID> heartbeat_check: no reply from <ADDR>"],
        ["NODE-A"],
        ["osd.5"],
    )
    assert identity.fault_family == "network_heartbeat"
    assert identity.entities == ("daemon:osd.5", "host:node-a")


def test_unknown_family_fails_closed():
    identity = derive_identity(["an unfamiliar message"], ["node-a"], ["osd.5"])
    assert identity.fault_family is None
    assert not same_semantic_problem(None, set(identity.entities), None, set(identity.entities))

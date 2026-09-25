"""Rejection branches of the typed action gateway and cluster selection.

These are the fail-closed paths of critical-path code (plan 5.1): every
malformed RBD copy/move/cleanup request, unsafe param shape, unverified
controlled action and unauthorised cluster selection must be refused.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from dashboard import cluster_scope
from shared.controlled_action_contract import evaluate_controlled_action
from worker.executor import action_contract
from worker.executor.action_contract import ActionContractError, validate_typed_action_params


TOKEN = "move-token-0123456789"
BASE = {"pool_name": "rbd-ssd", "image": "vm-disk-1", "dest_pool": "rbd-hdd", "dest_image": "vm-disk-1-moved",
        "size_bytes": 1024}
COPY = {"pool_name": "rbd-ssd", "image": "vm-disk-1", "snapshot": "snap-1", "dest_pool": "rbd-hdd",
        "dest_image": "vm-disk-1-copy", "size_bytes": 1024}
MOVE = {**BASE, "delete_source": True, "move_token": TOKEN, "checksum": "rbd-export-diff-sha256"}
CLEANUP = {**BASE, "delete_destination": True, "move_token": TOKEN, "checksum": "rbd-export-diff-sha256",
           "cleanup_of_action_id": "action-1"}


def test_valid_rbd_params_pass():
    validate_typed_action_params("rbd_copy_volume", COPY)
    validate_typed_action_params("rbd_move_volume", MOVE)
    validate_typed_action_params("rbd_move_cleanup_partial", CLEANUP)
    validate_typed_action_params("unrelated_action", {"anything": True})


@pytest.mark.parametrize("action_id,params", [
    ("rbd_copy_volume", {**COPY, "pool_name": "-bad pool"}),
    ("rbd_copy_volume", {**COPY, "snapshot": "snap/../x"}),
    ("rbd_copy_volume", {**COPY, "dest_pool": COPY["pool_name"]}),
    ("rbd_copy_volume", {**COPY, "image": "volume-1234"}),
    ("rbd_move_volume", {**MOVE, "dest_pool": "bad pool"}),
    ("rbd_move_volume", {**MOVE, "dest_image": "bad/image"}),
    ("rbd_move_volume", {**MOVE, "dest_pool": MOVE["pool_name"]}),
    ("rbd_move_volume", {**MOVE, "dest_image": MOVE["image"]}),
    ("rbd_move_volume", {**MOVE, "dest_image": "volume-abcd"}),
    ("rbd_move_volume", {**MOVE, "delete_source": False}),
    ("rbd_move_cleanup_partial", {**CLEANUP, "pool_name": "bad pool"}),
    ("rbd_move_cleanup_partial", {**CLEANUP, "image": "bad/image"}),
    ("rbd_move_cleanup_partial", {**CLEANUP, "dest_pool": CLEANUP["pool_name"]}),
    ("rbd_move_cleanup_partial", {**CLEANUP, "dest_image": CLEANUP["image"]}),
    ("rbd_move_cleanup_partial", {**CLEANUP, "delete_destination": False}),
])
def test_malformed_rbd_params_are_rejected(action_id, params):
    with pytest.raises(ActionContractError, match=action_id):
        validate_typed_action_params(action_id, params)


@pytest.mark.parametrize("params,message", [
    ({"a": {"b": {"c": {"d": {"e": {"f": {"g": 1}}}}}}}, "lồng quá sâu"),
    ({"": 1}, "key chuỗi"),
    ({"Command": "rm -rf /"}, "free-form command"),
    ({"items": [1, {"shell": "x"}]}, "free-form command"),
    ({"value": float("nan")}, "NaN"),
    ({"value": object()}, "JSON primitive"),
])
def test_unsafe_param_shapes_are_rejected(params, message):
    with pytest.raises(ValueError, match=message):
        action_contract._walk_params(params)


def _request(**overrides):
    payload = {
        "action_id": "rbd_resize_volume", "cluster_id": "cluster-1", "capability": "action.execute",
        "target_type": "volume", "target_id": "rbd-ssd/vm-disk-1", "params": {},
        "evidence_fingerprint": "a" * 64, "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        "actor": "operator",
    }
    payload.update(overrides)
    return action_contract.TypedActionRequest.model_validate(payload)


def test_typed_request_rejects_unknown_target_oversized_params_and_naive_expiry():
    assert _request(approval_fingerprint="B" * 64).approval_fingerprint == "b" * 64
    with pytest.raises(ValueError, match="target_type"):
        _request(target_type="datacenter")
    with pytest.raises(ValueError, match="kích thước"):
        _request(params={"blob": "x" * 20_000})
    with pytest.raises(ValueError, match="timezone"):
        _request(expires_at=datetime.now() + timedelta(minutes=5))
    with pytest.raises(ValueError, match="SHA-256"):
        _request(evidence_fingerprint="z" * 64)


def _controlled(**overrides):
    arguments = {"target_count": 1, "approved": True, "capability_verified": True, "target_verified": True,
                 "fresh_telemetry": True, "shadow_passed": True}
    arguments.update(overrides)
    return evaluate_controlled_action("rbd_resize_volume", **arguments)


@pytest.mark.parametrize("overrides,reason", [
    ({"target_count": 2}, "blast radius"),
    ({"capability_verified": False}, "preflight"),
    ({"target_verified": False}, "preflight"),
    ({"fresh_telemetry": False}, "fresh telemetry"),
    ({"shadow_passed": False}, "shadow/canary"),
    ({"approved": False}, "operator approval"),
])
def test_controlled_action_refuses_each_missing_precondition(overrides, reason):
    decision = _controlled(**overrides)
    assert decision.allowed is False
    assert reason in decision.reason
    assert decision.execution_command is None


def test_controlled_action_requires_allowlisted_action():
    decision = evaluate_controlled_action(
        "rbd_rm_everything", target_count=1, approved=True, capability_verified=True,
        target_verified=True, fresh_telemetry=True, shadow_passed=True,
    )
    assert decision.allowed is False
    assert decision.action is None


def _cluster(cluster_id, *, is_default=False):
    return SimpleNamespace(id=cluster_id, is_default=is_default)


def _http_request(user="viewer", **query):
    return SimpleNamespace(session={"user": user}, query_params=query)


def test_cluster_access_rejects_requested_cluster_without_grant(monkeypatch):
    monkeypatch.setattr(cluster_scope, "authorized_cluster_ids", lambda _user: {"b"})
    clusters = [_cluster("a", is_default=True), _cluster("b")]
    with pytest.raises(HTTPException) as denied:
        cluster_scope._enforce_request_cluster_access(_http_request(cluster="a"), clusters, clusters[0])
    assert denied.value.status_code == 403


def test_cluster_access_falls_back_to_first_granted_cluster(monkeypatch):
    monkeypatch.setattr(cluster_scope, "authorized_cluster_ids", lambda _user: {"b"})
    clusters = [_cluster("a", is_default=True), _cluster("b")]
    selected = cluster_scope._enforce_request_cluster_access(_http_request(), clusters, clusters[0])
    assert selected.id == "b"


def test_cluster_access_without_any_grant_is_refused(monkeypatch):
    monkeypatch.setattr(cluster_scope, "authorized_cluster_ids", lambda _user: {"gone"})
    clusters = [_cluster("a", is_default=True), _cluster("b")]
    with pytest.raises(HTTPException) as denied:
        cluster_scope._enforce_request_cluster_access(_http_request(), clusters, clusters[1])
    assert denied.value.status_code == 403


def test_admin_and_default_only_users_keep_their_selection(monkeypatch):
    clusters = [_cluster("a", is_default=True), _cluster("b")]
    monkeypatch.setattr(cluster_scope, "authorized_cluster_ids", lambda _user: None)
    assert cluster_scope._enforce_request_cluster_access(_http_request(cluster="b"), clusters, clusters[1]).id == "b"
    monkeypatch.setattr(cluster_scope, "authorized_cluster_ids", lambda _user: set())
    assert cluster_scope._enforce_request_cluster_access(_http_request(), clusters, clusters[0]).id == "a"

"""OSD/PG targets are resolved against the live cluster before execution (plan 6.1)."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import worker.llm.router_client as router_client
from shared.models import Cluster
from watcher.ceph_client import CephQueryError
from worker.executor.action_contract import ActionContractError


def _cluster():
    return Cluster(
        id="cluster-1", name="CS-LAB", ceph_mon_nodes="10.0.0.1,10.0.0.2", ssh_user="root",
        ssh_key_path="/root/.ssh/id_ed25519", ceph_exec_mode="cephadm", ceph_container_name="",
        is_default=True, is_active=True, ceph_mon_hostnames="", ceph_mgr_nodes="",
        ceph_osd_nodes="", ceph_rgw_nodes="",
    )


def _authorize(action_id, params, monkeypatch, ceph):
    monkeypatch.setattr(router_client, "run_ceph_json_command_with", ceph)
    router_client._authorize_typed_action_before_lease(
        cluster=_cluster(),
        action=SimpleNamespace(expires_at=datetime.now(timezone.utc) + timedelta(minutes=10)),
        action_id=action_id,
        target_nodes=[],
        action_params=params,
        preflight_allowed=True,
        remediation_case=SimpleNamespace(evidence_fingerprint="a" * 64),
    )


def _osds(*ids):
    def ceph(*args, **_kwargs):
        assert args[-1] == "ceph osd ls"
        assert args[0] == ["10.0.0.1", "10.0.0.2"]
        return "mon-a", list(ids)
    return ceph


def test_existing_osd_is_authorized(monkeypatch):
    _authorize("restart_osd_daemon", {"cephadm_osd_ids": [3]}, monkeypatch, _osds(0, 3, 7))


def test_osd_missing_from_the_live_cluster_is_refused(monkeypatch):
    with pytest.raises(ActionContractError):
        _authorize("restart_osd_daemon", {"cephadm_osd_ids": [42]}, monkeypatch, _osds(0, 3, 7))


def test_unreachable_cluster_fails_closed(monkeypatch):
    def down(*_args, **_kwargs):
        raise CephQueryError("All MON nodes failed")

    with pytest.raises(ActionContractError, match="không resolve được osd target"):
        _authorize("restart_osd_daemon", {"cephadm_osd_ids": [3]}, monkeypatch, down)


def _pgs(existing):
    calls = []

    def ceph(*args, **_kwargs):
        command = args[-1]
        calls.append(command)
        pg_id = command.rsplit(" ", 1)[1]
        if pg_id not in existing:
            raise CephQueryError(f"Error ENOENT: pgid '{pg_id}' does not exist")
        return "mon-a", {"pgid": pg_id, "up": [0, 1]}
    return ceph, calls


def test_live_pg_is_authorized_and_missing_pg_is_refused(monkeypatch):
    ceph, calls = _pgs({"2.1f"})
    _authorize("pg_repair_force", {"pg_ids": ["2.1f"]}, monkeypatch, ceph)
    assert calls == ["ceph pg map 2.1f"]
    ceph, _calls = _pgs({"2.1f"})
    with pytest.raises(ActionContractError):
        _authorize("pg_repair_force", {"pg_ids": ["9.ff"]}, monkeypatch, ceph)


def test_malformed_pg_id_is_never_sent_to_the_cluster(monkeypatch):
    ceph, calls = _pgs(set())
    with pytest.raises(ActionContractError):
        _authorize("pg_repair_force", {"pg_ids": ["2.1f; ceph osd pool rm x"]}, monkeypatch, ceph)
    assert calls == []

"""Cluster scope matrix: default, active secondary and disabled secondary (Plan readiness-gap-review R1, 9.4).

One cluster-scoped change (OSD replacement) is followed through every layer that decides the target
cluster: the page, the proposal, chat confirmation and the Worker's binding check.
"""

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from config.settings import settings
from dashboard.routes import chat as chat_route
from dashboard.routes import replace_osd
from shared import db as db_module
from shared.clusters import get_default_cluster_id
from shared.models import Action, Cluster, Incident
from worker.llm import router_client

CRUSH = {"crush": {"roots": [{"type": "root", "name": "default", "children": [
    {"type": "host", "name": "ceph2", "children": [{"type": "osd", "id": 2}]},
]}]}}


@pytest.fixture
def clusters(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "ceph_exec_mode", "cephadm")
    monkeypatch.setattr(replace_osd.cluster_snapshot, "read_section_snapshot",
                        lambda cluster_id, section: CRUSH if section == "crush" else None)
    with db_module.SessionLocal() as session:
        ids = {"default": get_default_cluster_id(session)}
        for name, active in (("secondary", True), ("disabled", False)):
            row = Cluster(name=f"matrix-{name}", ceph_mon_nodes="10.70.0.1", ceph_osd_nodes="10.70.0.2",
                          ssh_user="root", ssh_key_path="/k", ceph_exec_mode="cephadm",
                          is_default=False, is_active=active)
            session.add(row)
            session.commit()
            ids[name] = row.id
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    return SimpleNamespace(client=dashboard_client, ids=ids)


def _propose(client, cluster_id=None):
    url = "/replace-osd/propose" + (f"?cluster_id={cluster_id}" if cluster_id else "")
    return client.post(url, json={"osd_id": 2, "confirmation": "THAY osd.2"})


def _actions():
    with db_module.SessionLocal() as session:
        return [(a.id, session.get(Incident, a.incident_id).cluster_id) for a in session.query(Action).all()]


def test_a_change_on_a_disabled_cluster_is_refused_never_moved_to_the_default(clusters):
    assert _propose(clusters.client, clusters.ids["disabled"]).status_code == 409
    assert _actions() == []
    # A page still opens, on the default cluster (the header names it).
    assert clusters.client.get(f"/replace-osd?cluster_id={clusters.ids['disabled']}").status_code == 200


def test_a_change_from_a_session_whose_cluster_was_disabled_meanwhile_is_refused(clusters):
    clusters.client.get(f"/replace-osd?cluster_id={clusters.ids['secondary']}")  # the operator picks it
    with db_module.SessionLocal() as session:
        session.get(Cluster, clusters.ids["secondary"]).is_active = False      # someone disables it
        session.commit()

    assert _propose(clusters.client).status_code == 409
    assert _actions() == []


@pytest.mark.parametrize("which", ["default", "secondary"])
def test_a_change_on_an_active_cluster_belongs_to_that_cluster(clusters, which):
    response = _propose(clusters.client, clusters.ids[which])

    assert response.status_code == 201, response.text
    [(action_id, incident_cluster)] = _actions()
    assert incident_cluster == clusters.ids[which]
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        incident = session.get(Incident, action.incident_id)
        params = json.loads(action.action_params)
        assert params["_cluster_id"] == clusters.ids[which]
        assert router_client._cluster_binding_reason(session, incident, params) is None


def test_the_worker_and_chat_refuse_a_cluster_disabled_after_the_proposal(clusters):
    assert _propose(clusters.client, clusters.ids["secondary"]).status_code == 201
    with db_module.SessionLocal() as session:
        session.get(Cluster, clusters.ids["secondary"]).is_active = False
        session.commit()
        [(action_id, _)] = _actions()
        action = session.get(Action, action_id)
        reason = router_client._cluster_binding_reason(
            session, session.get(Incident, action.incident_id), json.loads(action.action_params))
        assert reason and "đã bị tắt" in reason

        message = SimpleNamespace(cluster_id=clusters.ids["secondary"], proposed_action_id="resync_ntp",
                                  proposed_target_nodes=json.dumps(["10.70.0.1"]), proposed_action_params=None,
                                  proposed_command_preview=None)
        with pytest.raises(HTTPException) as refused:
            chat_route._validate_chat_action_proposal(session, message)
        assert refused.value.status_code == 400

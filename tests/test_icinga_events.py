"""Icinga webhook (dashboard/routes/icinga.py, shared/icinga_events.py; plan icinga I1)."""

import json

import pytest

from config.settings import settings
from shared import db as db_module
from shared import icinga_events
from shared.models import Cluster, Incident
from shared.time import utc_now

TOKEN = "t" * 40
EVENT = {
    "notification_type": "PROBLEM", "host": "CS-STAGING", "service": "ceph-health", "state": "WARNING",
    "state_type": "HARD", "output": "WARNING - HEALTH_WARN: OSD_DOWN",
    "long_output": "OSD_DOWN (HEALTH_WARN): 1 osds down\nPG_DEGRADED (HEALTH_WARN): Degraded data redundancy",
    "last_state_change": "1760000000", "author": "", "comment": "", "event_id": "a" * 32,
}


@pytest.fixture
def webhook(dashboard_client, tmp_path, monkeypatch):
    token_file = tmp_path / "icinga-webhook.token"
    token_file.write_text(TOKEN + "\n")
    monkeypatch.setattr(settings, "icinga_webhook_token_file", str(token_file))
    monkeypatch.setattr(settings, "icinga_events_file", str(tmp_path / "icinga-events.jsonl"))
    return dashboard_client


def _post(client, body=EVENT, token=TOKEN):
    return client.post("/api/icinga/events", json=body, headers={"Authorization": f"Bearer {token}"})


def _records(tmp_path):
    return [json.loads(line) for line in (tmp_path / "icinga-events.jsonl").read_text().splitlines()]


def test_an_event_is_recorded_and_linked_to_the_matching_open_incident(webhook, tmp_path):
    with db_module.SessionLocal() as session:
        cluster = Cluster(name="CS-STAGING", ceph_mon_nodes="192.0.2.10", ssh_user="root", ssh_key_path="/k")
        session.add(cluster)
        session.flush()
        session.add(Incident(ceph_code="OSD_DOWN", status="NEW", cluster_id=cluster.id, detected_at=utc_now()))
        session.add(Incident(ceph_code="MON_DOWN", status="NEW", cluster_id=cluster.id, detected_at=utc_now()))
        session.commit()

    response = _post(webhook)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cluster_known"] is True and body["health_codes"] == ["OSD_DOWN", "PG_DEGRADED"]
    assert len(body["linked_incidents"]) == 1
    [stored] = _records(tmp_path)
    assert stored["linked_incidents"][0]["ceph_code"] == "OSD_DOWN" and stored["state"] == "WARNING"


def test_the_same_event_twice_is_recorded_once(webhook, tmp_path):
    assert _post(webhook).json()["duplicate"] is False
    assert _post(webhook).json()["duplicate"] is True
    assert len(_records(tmp_path)) == 1


@pytest.mark.parametrize("token", ["", "wrong" * 8, TOKEN[:-1]])
def test_a_wrong_token_is_refused_and_nothing_is_recorded(webhook, tmp_path, token):
    assert _post(webhook, token=token).status_code == 401
    assert not (tmp_path / "icinga-events.jsonl").exists()


def test_without_a_configured_token_the_webhook_is_off(webhook, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "icinga_webhook_token_file", str(tmp_path / "missing"))
    assert _post(webhook).status_code == 503


@pytest.mark.parametrize("change", [
    {"notification_type": "DROP TABLE"}, {"state": "BROKEN"}, {"event_id": "not-hex"},
    {"host": ""}, {"output": "x" * 5000},
])
def test_malformed_events_are_refused(webhook, change):
    assert _post(webhook, {**EVENT, **change}).status_code == 422


def test_an_unknown_host_is_recorded_without_a_cluster(webhook, tmp_path):
    body = _post(webhook, {**EVENT, "host": "OTHER", "event_id": "b" * 32}).json()
    assert body["cluster_known"] is False and body["linked_incidents"] == []


def test_only_the_webhook_path_skips_the_browser_csrf_checks():
    from dashboard import app
    assert app._TOKEN_AUTH_PATHS == frozenset({"/api/icinga/events"})


def test_health_codes_come_only_from_code_lines():
    event = icinga_events.IcingaEvent.model_validate(
        {**EVENT, "long_output": "OSD_DOWN (HEALTH_WARN): x\nnot a code line\nlower_case (x): y\nOSD_DOWN (HEALTH_WARN): again"})
    assert icinga_events.health_codes(event) == ["OSD_DOWN"]

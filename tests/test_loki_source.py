from datetime import datetime

import httpx

from shared.models import Cluster
from watcher.log_source import loki


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _cluster():
    return Cluster(name="CS-LAB", ceph_mon_nodes="10.0.0.1", ssh_user="root", ssh_key_path="/key")


def test_loki_rejects_stream_whose_labels_do_not_match_selector(monkeypatch):
    monkeypatch.setattr(loki.settings, "log_intel_loki_url", "http://loki")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Response({"data": {"result": [{
        "stream": {"cluster": "CS-LAB", "host": "wrong-host", "daemon_type": "osd"},
        "values": [["1787702400000000000", "osd.5 slow ops"]],
    }]}}))

    result = loki.fetch(
        "10.0.0.1", "osd", datetime(2026, 8, 25), datetime(2026, 8, 26), _cluster()
    )

    assert result.records == []
    assert "label không khớp" in result.error


def test_loki_accepts_exact_contract_labels(monkeypatch):
    monkeypatch.setattr(loki.settings, "log_intel_loki_url", "http://loki")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Response({"data": {"result": [{
        "stream": {"cluster": "CS-LAB", "host": "10.0.0.1", "daemon_type": "osd"},
        "values": [["1787702400000000000", "osd.5 slow ops"]],
    }]}}))

    result = loki.fetch(
        "10.0.0.1", "osd", datetime(2026, 8, 25), datetime(2026, 8, 26), _cluster()
    )

    assert len(result.records) == 1
    assert result.error is None

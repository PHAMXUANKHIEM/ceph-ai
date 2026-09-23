"""Elasticsearch adapter: bounded read, cluster isolation and failure posture."""

from datetime import datetime

import httpx

from config.settings import settings
from shared.models import Cluster
from watcher.log_source import elasticsearch, get_log_source


class Response:
    def __init__(self, payload=None, status=200):
        self.payload = payload or {}
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("failed", request=httpx.Request("POST", "http://es/_search"), response=httpx.Response(self.status_code))

    def json(self):
        return self.payload


def cluster():
    return Cluster(name="CS-LAB", ceph_mon_nodes="10.0.0.1", ssh_user="root", ssh_key_path="/key")


def setup(monkeypatch):
    monkeypatch.setattr(settings, "log_intel_elasticsearch_url", "http://es:9200")
    monkeypatch.setattr(settings, "log_intel_elasticsearch_index", "ceph-logs-*")
    monkeypatch.setattr(settings, "log_intel_elasticsearch_api_key", "secret-key")


def test_factory_and_bounded_exact_query(monkeypatch):
    setup(monkeypatch)
    monkeypatch.setattr(settings, "log_intel_max_lines_per_daemon", 100000)
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response({"hits": {"hits": [{"_source": {
            "@timestamp": "2026-08-25T12:00:00Z", "cluster": "CS-LAB",
            "host": "10.0.0.1", "daemon_type": "osd", "message": "osd.5 slow ops",
        }}]}})
    monkeypatch.setattr(httpx, "post", post)
    assert get_log_source("elasticsearch") is elasticsearch
    result = elasticsearch.fetch("10.0.0.1", "osd", datetime(2026, 8, 25), datetime(2026, 8, 26), cluster())
    assert len(result.records) == 1
    assert result.records[0].ts == datetime(2026, 8, 25, 12)
    url, options = calls[0]
    assert url == "http://es:9200/ceph-logs-*/_search"
    assert options["headers"]["Authorization"] == "ApiKey secret-key"
    assert options["json"]["size"] <= 5000
    filters = options["json"]["query"]["bool"]["filter"]
    assert {"term": {"cluster.keyword": "CS-LAB"}} in filters
    assert {"term": {"host.keyword": "10.0.0.1"}} in filters


def test_mismatched_cluster_and_bad_timestamp_are_partial(monkeypatch):
    setup(monkeypatch)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: Response({"hits": {"hits": [
        {"_source": {"@timestamp": "2026-08-25T12:00:00Z", "cluster": "OTHER", "host": "10.0.0.1", "daemon_type": "osd", "message": "wrong"}},
        {"_source": {"@timestamp": "bad", "cluster": "CS-LAB", "host": "10.0.0.1", "daemon_type": "osd", "message": "bad"}},
    ]}}))
    result = elasticsearch.fetch("10.0.0.1", "osd", datetime(2026, 8, 25), datetime(2026, 8, 26), cluster())
    assert result.records == []
    assert "2 document" in result.error


def test_query_failure_does_not_leak_key(monkeypatch):
    setup(monkeypatch)
    def fail(*args, **kwargs):
        raise RuntimeError("secret-key in URL")
    monkeypatch.setattr(httpx, "post", fail)
    result = elasticsearch.fetch("10.0.0.1", "osd", datetime(2026, 8, 25), datetime(2026, 8, 26), cluster())
    assert result.records == []
    assert "secret-key" not in result.error


def test_rejects_untrusted_index_before_request(monkeypatch):
    setup(monkeypatch)
    monkeypatch.setattr(settings, "log_intel_elasticsearch_index", "../../_all")
    result = elasticsearch.fetch("10.0.0.1", "osd", datetime(2026, 8, 25), datetime(2026, 8, 26), cluster())
    assert "index pattern" in result.error


def test_partial_shard_failure_is_not_silent(monkeypatch):
    setup(monkeypatch)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: Response({"_shards": {"failed": 1}, "hits": {"hits": []}}))
    result = elasticsearch.fetch("10.0.0.1", "osd", datetime(2026, 8, 25), datetime(2026, 8, 26), cluster())
    assert "thất bại" in result.error

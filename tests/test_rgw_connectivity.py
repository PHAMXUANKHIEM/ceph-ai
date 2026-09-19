from watcher.rgw_connectivity import probe_endpoint


def test_probe_rejects_endpoint_outside_configured_allowlist():
    result = probe_endpoint("https://not-rgw.example", allowed_hosts={"rgw-1"})

    assert result["status"] == "not_available"
    assert result["dns"] == "not_attempted"
    assert "không nằm" in result["error"]


def test_probe_reports_dns_failure_without_network_guess(monkeypatch):
    def fail(*_args, **_kwargs):
        raise OSError("no dns")

    monkeypatch.setattr("watcher.rgw_connectivity.socket.getaddrinfo", fail)
    result = probe_endpoint("http://rgw-1:7480", allowed_hosts={"rgw-1"})

    assert result["status"] == "not_available"
    assert result["dns"] == "failed"
    assert result["tcp"] == "not_attempted"

from watcher.rgw_bucket_diagnosis import build_bucket_access_diagnosis


def test_diagnosis_uses_dns_tls_and_daemon_error_evidence():
    result = build_bucket_access_diagnosis(
        [{"status": 200}],
        bucket="archive",
        endpoint_probes=[{
            "endpoint": "https://rgw.example",
            "dns": "ok", "tcp": "ok", "tls": "failed",
        }],
        daemon_errors=[{"message": "backend connection refused"}],
        rgw_evidence={"endpoints": {"status": "observed"}},
    )

    codes = {finding["code"] for finding in result["findings"]}
    assert "RGW_TLS_FAILURE" in codes
    assert "RGW_BACKEND_CONNECTION_ERROR" in codes
    assert result["daemon_errors"][0]["message"] == "backend connection refused"
    assert result["action_id"] is None

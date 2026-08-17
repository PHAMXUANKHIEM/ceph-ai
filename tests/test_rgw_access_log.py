import watcher.rgw_access_log as ral

# Real example line verified against ceph/ceph#33083 (the PR that added
# Beast's access log) — see watcher/rgw_access_log.py's own docstring.
HEAD_LINE = (
    "2024-06-12T13:10:07.404+0000 7fc49be9a710  1 beast: 0x7fc49be9a710: 10.0.12.5 - anonymous "
    '[12/Jun/2024:13:10:07.404 +0000] "HEAD / HTTP/1.0" 200 5 - - - latency=0.000000000s'
)
GET_OBJECT_LINE = (
    "2024-06-12T13:11:00.000+0000 7fc49be9a710  1 beast: 0x7fc49be9a710: 10.20.1.5 - operator "
    '[12/Jun/2024:13:11:00.000 +0000] "GET /my-bucket/photo.jpg HTTP/1.1" 200 1024 - - - '
    "latency=0.010000000s"
)
PUT_OBJECT_LINE = (
    "2024-06-12T13:12:00.000+0000 7fc49be9a710  1 beast: 0x7fc49be9a710: 10.20.1.6 - operator "
    '[12/Jun/2024:13:12:00.000 +0000] "PUT /my-bucket/photo.jpg HTTP/1.1" 200 0 - - - '
    "latency=0.020000000s"
)
DELETE_OBJECT_LINE = (
    "2024-06-12T13:13:00.000+0000 7fc49be9a710  1 beast: 0x7fc49be9a710: 10.20.1.7 - operator "
    '[12/Jun/2024:13:13:00.000 +0000] "DELETE /my-bucket/photo.jpg HTTP/1.1" 204 0 - - - '
    "latency=0.005000000s"
)
GET_BUCKET_LINE = (
    "2024-06-12T13:14:00.000+0000 7fc49be9a710  1 beast: 0x7fc49be9a710: 10.20.1.8 - operator "
    '[12/Jun/2024:13:14:00.000 +0000] "GET /my-bucket/ HTTP/1.1" 200 512 - - - latency=0.001s'
)
FORBIDDEN_LINE = (
    "2024-06-12T13:15:00.000+0000 7fc49be9a710  1 beast: 0x7fc49be9a710: 10.20.1.9 - anonymous "
    '[12/Jun/2024:13:15:00.000 +0000] "GET /other-bucket/secret.txt HTTP/1.1" 403 0 - - - '
    "latency=0.001s"
)
NON_BEAST_LINE = "2024-06-12T13:16:00.000+0000 7fc49be9a710  1 some unrelated log line"


def test_parse_head_service_root_line():
    records = ral.parse_beast_access_log(HEAD_LINE)

    assert len(records) == 1
    r = records[0]
    assert r["remote_addr"] == "10.0.12.5"
    assert r["method"] == "HEAD"
    assert r["status"] == 200
    assert r["bucket"] is None
    assert r["object"] is None
    assert r["action"] == "Kiểm tra Bucket"  # HEAD, no object -> bucket-level
    assert r["timestamp"] is not None
    assert r["timestamp"].year == 2024


def test_parse_get_object_line_is_download():
    records = ral.parse_beast_access_log(GET_OBJECT_LINE)

    r = records[0]
    assert r["bucket"] == "my-bucket"
    assert r["object"] == "photo.jpg"
    assert r["action"] == "Tải xuống"
    assert r["remote_addr"] == "10.20.1.5"
    assert r["status"] == 200


def test_parse_put_object_line_is_upload():
    records = ral.parse_beast_access_log(PUT_OBJECT_LINE)

    assert records[0]["action"] == "Tải lên"
    assert records[0]["bucket"] == "my-bucket"
    assert records[0]["object"] == "photo.jpg"


def test_parse_delete_object_line():
    records = ral.parse_beast_access_log(DELETE_OBJECT_LINE)

    assert records[0]["action"] == "Xoá tệp"
    assert records[0]["status"] == 204


def test_parse_get_bucket_root_is_listing_not_download():
    records = ral.parse_beast_access_log(GET_BUCKET_LINE)

    r = records[0]
    assert r["bucket"] == "my-bucket"
    assert r["object"] is None
    assert r["action"] == "Liệt kê"


def test_parse_forbidden_status_code_preserved():
    records = ral.parse_beast_access_log(FORBIDDEN_LINE)

    assert records[0]["status"] == 403


def test_parse_skips_non_beast_lines():
    assert ral.parse_beast_access_log(NON_BEAST_LINE) == []


def test_parse_multiple_lines_sorted_newest_first():
    raw = "\n".join([HEAD_LINE, GET_OBJECT_LINE, PUT_OBJECT_LINE])

    records = ral.parse_beast_access_log(raw)

    assert len(records) == 3
    # PUT (13:12:00) is the newest of the three -> first.
    assert records[0]["method"] == "PUT"
    assert records[-1]["method"] == "HEAD"


def test_parse_ignores_non_matching_lines_mixed_with_real_ones():
    raw = "\n".join([NON_BEAST_LINE, GET_OBJECT_LINE, "another unrelated line"])

    records = ral.parse_beast_access_log(raw)

    assert len(records) == 1
    assert records[0]["method"] == "GET"


# --- fetch_bucket_access_log() -------------------------------------------


def test_fetch_bucket_access_log_passes_bucket_as_grep_filter(monkeypatch):
    captured = {}

    def fake_fetch(host, filter_text):
        captured["host"] = host
        captured["filter_text"] = filter_text
        return GET_OBJECT_LINE

    monkeypatch.setattr(ral, "fetch_rgw_log", fake_fetch)

    records = ral.fetch_bucket_access_log("10.20.1.90", "my-bucket")

    assert captured == {"host": "10.20.1.90", "filter_text": "my-bucket"}
    assert len(records) == 1
    assert records[0]["bucket"] == "my-bucket"


def test_fetch_bucket_access_log_precisely_filters_out_substring_false_positive(monkeypatch):
    # The coarse server-side grep for "bucket" would match BOTH lines (the
    # bucket name text appears in both paths) — the precise post-parse
    # filter must only keep the one whose PARSED bucket field actually
    # equals "my-bucket".
    raw = "\n".join([GET_OBJECT_LINE, FORBIDDEN_LINE])
    monkeypatch.setattr(ral, "fetch_rgw_log", lambda host, filter_text: raw)

    records = ral.fetch_bucket_access_log("10.20.1.90", "my-bucket")

    assert len(records) == 1
    assert records[0]["bucket"] == "my-bucket"


def test_fetch_bucket_access_log_no_bucket_filter_returns_everything(monkeypatch):
    raw = "\n".join([GET_OBJECT_LINE, FORBIDDEN_LINE])
    monkeypatch.setattr(ral, "fetch_rgw_log", lambda host, filter_text: raw)

    records = ral.fetch_bucket_access_log("10.20.1.90")

    assert len(records) == 2


def test_fetch_bucket_access_log_propagates_rgw_log_error(monkeypatch):
    def raising(host, filter_text):
        raise ral.RgwLogError("unreachable")

    monkeypatch.setattr(ral, "fetch_rgw_log", raising)

    try:
        ral.fetch_bucket_access_log("10.20.1.90")
        assert False, "expected RgwLogError"
    except ral.RgwLogError:
        pass


# --- fetch_bucket_stats() / summarize_bucket_stats() ----------------------

RADOSGW_ADMIN_BUCKET_STATS_JSON = """{
  "bucket": "my-bucket",
  "num_shards": 11,
  "tenant": "",
  "owner": "operator",
  "placement_rule": "default-placement",
  "id": "abc123",
  "creation_time": "2024-01-15 08:30:00.000000",
  "usage": {
    "rgw.main": {
      "size": 1048576,
      "size_utilized": 1048576,
      "num_objects": 42
    }
  },
  "bucket_quota": {
    "enabled": true,
    "max_size": 10737418240,
    "max_objects": 1000
  }
}"""


def test_fetch_bucket_stats_builds_docker_exec_command_against_rgw_container(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "ceph_exec_mode", "docker", raising=False)
    monkeypatch.setattr(settings, "ceph_rgw_container_name", "ceph-rgw-B", raising=False)
    captured = {}

    def fake_run(host, command):
        captured["host"] = host
        captured["command"] = command
        return RADOSGW_ADMIN_BUCKET_STATS_JSON

    monkeypatch.setattr(ral, "run_command_on_node", fake_run)

    result = ral.fetch_bucket_stats("10.20.1.90", "my-bucket")

    assert captured["host"] == "10.20.1.90"
    assert captured["command"] == (
        "docker exec ceph-rgw-B radosgw-admin bucket stats --bucket=my-bucket --format json"
    )
    assert result["owner"] == "operator"
    assert result["usage"]["rgw.main"]["num_objects"] == 42


def test_summarize_bucket_stats_reports_optional_capabilities_without_guessing():
    base = ral.summarize_bucket_stats({"usage": {}, "bucket_quota": {}})
    assert base["versioning_status"] == "unknown"
    assert base["object_lock_status"] == "unknown"
    assert base["policy_available"] is False
    assert base["lifecycle_available"] is False

    enriched = ral.summarize_bucket_stats({
        "versioning": {"status": "Enabled"},
        "object_lock_enabled": False,
        "policy": {"Statement": []},
        "lifecycle": [],
    })
    assert enriched["versioning_status"] == "enabled"
    assert enriched["object_lock_status"] == "disabled"
    assert enriched["policy_available"] is True
    assert enriched["lifecycle_available"] is True


def test_summarize_s3_user_discards_all_key_material():
    summary = ral.summarize_s3_user({
        "user_id": "alice",
        "keys": [{"access_key": "AKIA", "secret_key": "SECRET"}],
        "caps": [{"type": "users", "perm": "read"}],
    })
    assert summary["uid"] == "alice"
    assert summary["key_count"] == 1
    assert summary["caps"] == ["users"]
    assert "AKIA" not in str(summary)
    assert "SECRET" not in str(summary)


def test_s3_user_action_builder_is_closed_quotes_input_and_never_generates_key():
    command = ral.build_s3_user_action_command(
        "create", "alice tenant", {"display_name": "Alice's Team", "email": "a@example.test"}
    )
    assert "--uid='alice tenant'" in command
    assert "--generate-key=false" in command
    assert "--display-name='Alice'\"'\"'s Team'" in command

    try:
        ral.build_s3_user_action_command("delete", "alice", {})
        assert False, "unsupported actions must fail closed"
    except ValueError:
        pass


def test_fetch_bucket_stats_docker_mode_requires_container_name(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "ceph_exec_mode", "docker", raising=False)
    monkeypatch.setattr(settings, "ceph_rgw_container_name", "", raising=False)

    try:
        ral.fetch_bucket_stats("10.20.1.90", "my-bucket")
        assert False, "expected RgwLogError"
    except ral.RgwLogError as exc:
        assert "container" in str(exc).lower()


def test_fetch_bucket_stats_cephadm_mode_uses_shell_wrapper_no_container_needed(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "ceph_exec_mode", "cephadm", raising=False)
    monkeypatch.setattr(settings, "ceph_rgw_container_name", "", raising=False)
    captured = {}

    def fake_run(host, command):
        captured["command"] = command
        return RADOSGW_ADMIN_BUCKET_STATS_JSON

    monkeypatch.setattr(ral, "run_command_on_node", fake_run)

    result = ral.fetch_bucket_stats("10.20.1.90", "my-bucket")

    assert captured["command"] == "cephadm shell -- radosgw-admin bucket stats --bucket=my-bucket --format json"
    assert result["owner"] == "operator"


def test_fetch_bucket_stats_returns_none_for_unknown_bucket(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "ceph_exec_mode", "cephadm", raising=False)
    monkeypatch.setattr(ral, "run_command_on_node", lambda host, command: "ERROR: could not fetch bucket info")

    assert ral.fetch_bucket_stats("10.20.1.90", "no-such-bucket") is None


def test_fetch_bucket_stats_raises_rgw_log_error_on_ssh_failure(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "ceph_exec_mode", "cephadm", raising=False)

    def raising(host, command):
        raise OSError("no route to host")

    monkeypatch.setattr(ral, "run_command_on_node", raising)

    try:
        ral.fetch_bucket_stats("10.20.1.90", "my-bucket")
        assert False, "expected RgwLogError"
    except ral.RgwLogError:
        pass


def test_fetch_bucket_stats_shell_quotes_bucket_name(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "ceph_exec_mode", "cephadm", raising=False)
    captured = {}

    def fake_run(host, command):
        captured["command"] = command
        return RADOSGW_ADMIN_BUCKET_STATS_JSON

    monkeypatch.setattr(ral, "run_command_on_node", fake_run)

    ral.fetch_bucket_stats("10.20.1.90", "bucket; rm -rf /")

    assert "'bucket; rm -rf /'" in captured["command"]


def test_summarize_bucket_stats_extracts_display_fields():
    import json

    raw = json.loads(RADOSGW_ADMIN_BUCKET_STATS_JSON)

    summary = ral.summarize_bucket_stats(raw)

    assert summary["owner"] == "operator"
    assert summary["creation_time"].year == 2024
    assert summary["creation_time"].month == 1
    assert summary["creation_time"].day == 15
    assert summary["num_objects"] == 42
    assert summary["size_bytes"] == 1048576
    assert summary["quota_enabled"] is True
    assert summary["quota_max_size_bytes"] == 10737418240
    assert summary["quota_max_objects"] == 1000


def test_summarize_bucket_stats_handles_empty_bucket_with_no_usage_category():
    raw = {"owner": "operator", "creation_time": None, "usage": {}, "bucket_quota": {"enabled": False}}

    summary = ral.summarize_bucket_stats(raw)

    assert summary["num_objects"] == 0
    assert summary["size_bytes"] == 0
    assert summary["creation_time"] is None
    assert summary["quota_enabled"] is False

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

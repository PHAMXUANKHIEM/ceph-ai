from datetime import datetime, timezone

from dashboard.vntime import format_vn, format_vn_clock, to_utc_iso


def test_to_utc_iso_appends_z_for_naive_datetime():
    # Naive datetimes are always UTC in this codebase (datetime.utcnow()) —
    # the "Z" suffix is what makes `new Date(iso)` on the frontend parse it
    # as UTC instead of (per the ECMAScript spec) silently assuming a
    # timezone-less string is already browser-local time.
    dt = datetime(2026, 7, 23, 3, 29, 30, 58514)
    assert to_utc_iso(dt) == "2026-07-23T03:29:30.058514Z"


def test_to_utc_iso_handles_already_aware_utc_datetime():
    dt = datetime(2026, 7, 23, 3, 29, 30, tzinfo=timezone.utc)
    assert to_utc_iso(dt) == "2026-07-23T03:29:30Z"


def test_format_vn_converts_naive_utc_to_vietnam_local_time():
    # 03:29 UTC -> 10:29 ICT (UTC+7, no DST).
    dt = datetime(2026, 7, 23, 3, 29, 30)
    assert format_vn(dt) == "23/07/2026 10:29:30"


def test_format_vn_wraps_past_midnight_correctly():
    # 20:00 UTC -> 03:00 ICT the NEXT calendar day — a naive "+7 hours on
    # the string" approach would get the date wrong here, real timezone
    # conversion must not.
    dt = datetime(2026, 7, 22, 20, 0, 0)
    assert format_vn(dt) == "23/07/2026 03:00:00"


def test_format_vn_returns_placeholder_for_none():
    assert format_vn(None) == "—"


def test_format_vn_clock_converts_naive_utc_to_vietnam_local_time():
    dt = datetime(2026, 7, 23, 3, 29, 30)
    assert format_vn_clock(dt) == "10:29:30"


def test_format_vn_clock_wraps_past_midnight_correctly():
    dt = datetime(2026, 7, 22, 20, 0, 0)
    assert format_vn_clock(dt) == "03:00:00"


def test_format_vn_clock_returns_none_for_none():
    # Deliberately None, not "—" like format_vn — the Deploy Cluster live
    # log (dashboard/routes/deploy_cluster.py) uses this to decide whether
    # to print a time prefix at all for a step that hasn't happened yet,
    # not to print a placeholder that reads like a real timestamp.
    assert format_vn_clock(None) is None

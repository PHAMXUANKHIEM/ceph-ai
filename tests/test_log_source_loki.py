from datetime import datetime, timedelta, timezone

from watcher.log_source import loki


def test_naive_pipeline_datetime_is_interpreted_as_utc(monkeypatch):
    # Must be independent of the machine running the test. Production runs
    # in Asia/Ho_Chi_Minh, where naive datetime.timestamp() shifted queries
    # seven hours away from the requested evidence window.
    monkeypatch.setenv("TZ", "Asia/Ho_Chi_Minh")
    naive = datetime(2026, 8, 21, 5, 47, 0)
    expected = datetime(2026, 8, 21, 5, 47, 0, tzinfo=timezone.utc)
    assert loki._utc_nanoseconds(naive) == str(int(expected.timestamp() * 1_000_000_000))


def test_aware_datetime_preserves_its_offset():
    utc = datetime(2026, 8, 21, 5, 47, 0, tzinfo=timezone.utc)
    plus_seven = utc.astimezone(timezone(timedelta(hours=7)))
    assert loki._utc_nanoseconds(plus_seven) == loki._utc_nanoseconds(utc)

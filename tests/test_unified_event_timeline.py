"""Tests for the unified event timeline merger."""

from shared.unified_event_timeline import merge_event_sources


def test_merges_sources_in_time_order_and_deduplicates():
    result = merge_event_sources({
        "incident": [{"id": "1", "at": "2026-09-21T10:00:00Z", "kind": "detected"}],
        "audit": [{"id": "2", "at": "2026-09-21T10:01:00Z", "event_type": "approved"}],
    })

    assert result["status"] == "observed"
    assert [event["source"] for event in result["events"]] == ["incident", "audit"]
    assert result["summary"]["event_count"] == 2
    assert result["read_only"] is True


def test_invalid_time_is_an_evidence_gap():
    result = merge_event_sources({
        "incident": [{"id": "bad", "at": "not-a-time"}],
    })

    assert result["events"] == []
    assert result["status"] == "not_available"
    assert any(gap["code"] == "EVENT_TIMESTAMP_INVALID"
               for gap in result["evidence_gaps"])


def test_bounds_events_and_does_not_leak_sensitive_fields():
    result = merge_event_sources({
        "audit": [{
            "id": "1",
            "at": "2026-09-21T10:00:00Z",
            "event_type": "rotate",
            "secret_key": "MUST-NOT-LEAK",
        }],
    }, limit=1)

    assert "MUST-NOT-LEAK" not in repr(result)
    assert len(result["events"]) == 1
    assert result["summary"]["truncated"] is False

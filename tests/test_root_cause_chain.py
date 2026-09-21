"""Tests for citation-linked root-cause chains."""

from shared.root_cause_chain import build_root_cause_chain


def test_root_candidate_and_response_keep_event_citations():
    result = build_root_cause_chain({
        "incident_id": "inc-1",
        "ceph_code": "OSD_DOWN",
        "events": [
            {"id": "event:detected", "at": "2026-09-21T10:00:00Z",
             "kind": "health_transition", "summary": "OSD down"},
            {"id": "event:action", "at": "2026-09-21T10:01:00Z",
             "kind": "action_executed", "summary": "restart"},
        ],
    })

    assert result["status"] == "observed"
    assert result["chain"][0]["role"] == "root_candidate"
    assert result["chain"][1]["role"] == "response_or_effect"
    assert result["hypotheses"][0]["citations"] == ["event:detected"]


def test_lifecycle_only_chain_is_uncertain():
    result = build_root_cause_chain({
        "events": [{"id": "event:1", "at": "2026-09-21T10:00:00Z",
                    "kind": "approved", "summary": "approved"}],
    })

    assert result["hypotheses"][0]["confidence"] < 0.5
    assert "ROOT_SIGNAL_NOT_OBSERVED" in {
        gap["code"] for gap in result["evidence_gaps"]
    }


def test_empty_timeline_fails_closed():
    result = build_root_cause_chain({"events": []})
    assert result["status"] == "not_available"
    assert result["hypotheses"] == []
    assert result["read_only"] is True

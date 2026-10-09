"""FL6.2: AI reproduction proposals are parsed and validated fail-closed."""

import json

import pytest

from shared import reproduction_proposal as rp

URL = "https://docs.ceph.com/en/latest/rados/operations/health-checks/#osd-down"
PREFIXES = ("OSD_DOWN", "MON_DOWN", "MGR_DOWN", "OSD_HOST_DOWN")
ACTIONS = {"investigate_manually", "restart_osd_daemon"}


def _raw(**overrides):
    value = {"family": "network_heartbeat", "fault_kind": "stop_osd", "parameters": {},
             "expected_health_codes": ["OSD_DOWN", "PG_DEGRADED"],
             "cause": "Một OSD daemon dừng nên MON đánh dấu OSD down và PG bị degraded.",
             "acceptable_action_ids": ["investigate_manually", "restart_osd_daemon"], "max_seconds": 300,
             "citations": [URL], "reasoning": "Tài liệu OSD_DOWN nêu daemon dừng.", "new_kind_request": {}}
    value.update(overrides)
    return value


def _validate(raw):
    return rp.validate(raw, family="network_heartbeat", given_urls={URL}, action_ids=ACTIONS, family_prefixes=PREFIXES)


def test_a_sound_proposal_passes():
    proposal, errors = _validate(_raw())

    assert errors == [] and proposal.fault_kind == "stop_osd" and proposal.expected_health_codes == ["OSD_DOWN", "PG_DEGRADED"]


@pytest.mark.parametrize(("change", "reason"), [
    ({"fault_kind": "rm -rf /"}, "unknown fault kind"),
    ({"parameters": {"osd": 3}}, "take no parameters"),
    ({"expected_health_codes": ["OSD_NEARFULL"]}, "cannot raise"),
    ({"expected_health_codes": ["PG_DEGRADED"]}, "belongs to network_heartbeat"),
    ({"acceptable_action_ids": ["purge_osd"]}, "outside the catalog"),
    ({"max_seconds": 7200}, "max_seconds"),
    ({"citations": ["https://example.com/blog"]}, "citation was not among"),
    ({"cause": "osd"}, "cause is missing"),
    ({"family": "pg_peering"}, "family differs"),
])
def test_anything_outside_the_runner_or_catalog_is_refused(change, reason):
    proposal, errors = _validate(_raw(**change))

    assert proposal is None and any(reason in error for error in errors), errors


def test_a_new_kind_request_is_kept_but_never_runnable():
    request = {"name": "slow_bluestore", "mechanism": "throttle", "undo": "remove throttle", "risk": "low"}
    proposal, errors = _validate(_raw(fault_kind=rp.NEW_KIND, new_kind_request=request))

    assert proposal is not None and proposal.new_kind_request == request
    assert errors == ["new fault kind requested: code change and review, never run"]


def test_the_answer_json_is_found_inside_prose_and_bad_answers_are_empty():
    assert rp.parse("Đề xuất:\n```json\n" + json.dumps(_raw()) + "\n```")["fault_kind"] == "stop_osd"
    assert rp.parse("no json here") == {} and rp.parse("{broken") == {}


def test_the_prompt_forbids_commands_and_lists_only_given_documents():
    prompt = rp.build_prompt("network_heartbeat", ["osd.1 down"], [{"url": URL, "text": "OSD_DOWN ..."}],
                             sorted(ACTIONS))

    assert "KHÔNG viết lệnh shell" in prompt and URL in prompt and "restart_osd_daemon" in prompt
    assert "stop_osd" in prompt and rp.NEW_KIND in prompt


def test_propose_stores_a_validated_proposal(monkeypatch, tmp_path):
    from scripts.lab import propose_reproduction as cli

    page = '<section id="osd-down"><h3>OSD_DOWN</h3><p>One or more OSDs are marked down.</p></section>'
    monkeypatch.setattr("shared.evidence_gaps.evidence_gap_queue", lambda *a, **k: [])
    record = cli.propose("network_heartbeat", ask=lambda prompt: json.dumps(_raw()), page=page)

    assert record["status"] == "PROPOSED" and record["documents"] == [URL]
    saved = json.loads(cli.save(record, tmp_path).read_text())
    assert saved["proposal"]["fault_kind"] == "stop_osd"


def test_an_invalid_answer_is_stored_as_rejected_with_reasons(monkeypatch):
    from scripts.lab import propose_reproduction as cli

    monkeypatch.setattr("shared.evidence_gaps.evidence_gap_queue", lambda *a, **k: [])
    record = cli.propose("network_heartbeat", ask=lambda prompt: "Tôi đề xuất chạy ceph osd down 3", page="")

    assert record["status"] == "REJECTED" and record["validation_errors"]

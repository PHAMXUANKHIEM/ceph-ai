import json

from shared.deploy_status import deploy_status, deploy_status_text
from shared.natural_language.router import route_natural_language


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_a_deploy_question_lists_the_last_run_the_queue_and_approved_merges_still_waiting(tmp_path):
    _write(tmp_path / "status.json", {"state": "succeeded", "sha": "9c81f547" + "0" * 32,
                                      "updated_at": "2026-10-10T02:04:31+00:00", "message": "5 phút"})
    _write(tmp_path / "pending.json", {"sha": "1b259a5e" + "0" * 32, "requested_by": "telegram:op",
                                       "requested_at": "2026-10-10T03:00:00+00:00"})
    _write(tmp_path / "approved" / ("a" * 40 + ".json"), {"pr": 44, "title": "fix(trash): cache",
                                                          "approved_at": "2026-10-10T02:30:00+00:00"})
    _write(tmp_path / "approved" / ("b" * 40 + ".json"), {"pr": 40, "title": "older, already deployed",
                                                          "approved_at": "2026-10-09T20:00:00+00:00"})
    _write(tmp_path / "held" / ("c" * 40 + ".json"), {"pr": 21})

    view = deploy_status(tmp_path)
    text = deploy_status_text(tmp_path)

    assert [row["pr"] for row in view["approved_not_deployed"]] == [44]
    assert "✅ deploy gần nhất thành công: 9c81f547" in text and "(5 phút)" in text
    assert "⏳ Đang xếp hàng: 1b259a5e, yêu cầu bởi telegram:op" in text
    assert "1 bản đã duyệt merge, chưa deploy" in text and "PR #44 fix(trash): cache" in text
    assert "1 bản duyệt đang bị giữ lại" in text


def test_a_running_deploy_and_an_empty_directory_read_plainly(tmp_path):
    assert "Chưa có lượt deploy nào được ghi nhận." in deploy_status_text(tmp_path)
    _write(tmp_path / "status.json", {"state": "running", "sha": "d" * 40, "updated_at": "2026-10-10T03:00:00Z"})
    text = deploy_status_text(tmp_path)
    assert "🚀 ĐANG DEPLOY: dddddddd" in text and "Không có bản nào đang chờ" not in text


def test_the_router_reads_deploy_questions_and_blocks_deploy_commands():
    assert route_natural_language("phần deploy có đang làm không", cluster_id="c").intent == "deploy_status"
    for command in ("deploy đi", "merge PR rồi deploy", "rollback bản trước"):
        routed = route_natural_language(command, cluster_id="c")
        assert routed.decision_reason == "mutation_language_blocked", command

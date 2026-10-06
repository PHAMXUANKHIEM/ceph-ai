"""Host self-check (plans SM2, SM3): alert rules, self-heal, checks and packaging."""

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.selfcheck import ceph_ai_selfcheck as selfcheck

ROOT = Path(__file__).resolve().parents[1]
T0 = 1_800_000_000.0


def _evaluate(results, state, now, in_grace=False):
    return selfcheck.evaluate(results, state, now, in_grace=in_grace)


def test_a_problem_is_reported_after_two_runs_then_reminded_and_recovered():
    state: dict = {}
    assert _evaluate({"rabbitmq": "không trả lời ping"}, state, T0) == []
    assert _evaluate({"rabbitmq": "không trả lời ping"}, state, T0 + 60) == ["🔴 rabbitmq: không trả lời ping"]
    assert _evaluate({"rabbitmq": "không trả lời ping"}, state, T0 + 120) == []
    reminder = _evaluate({"rabbitmq": "không trả lời ping"}, state, T0 + 60 + selfcheck.REMIND_SECONDS)
    assert reminder and reminder[0].startswith("🔴 rabbitmq: vẫn lỗi sau 6 giờ")
    recovered = _evaluate({"rabbitmq": None}, state, T0 + 7 * 3600)
    assert recovered == ["✅ rabbitmq: đã hồi phục sau 7 giờ 0 phút"]
    assert _evaluate({"rabbitmq": None}, state, T0 + 7 * 3600 + 60) == []


def test_a_single_blip_is_never_reported():
    state: dict = {}
    assert _evaluate({"dashboard": "HTTP 502"}, state, T0) == []
    assert _evaluate({"dashboard": None}, state, T0 + 60) == []


def test_nothing_is_reported_during_the_boot_grace_period():
    state: dict = {}
    for minute in range(5):
        assert _evaluate({"container:ceph-ai_worker_1": "không chạy (created)"}, state, T0 + 60 * minute, in_grace=True) == []
    assert _evaluate({"container:ceph-ai_worker_1": "không chạy (created)"}, state, T0 + 600) == [
        "🔴 container:ceph-ai_worker_1: không chạy (created)"
    ]


def test_one_boot_summary_per_reboot_but_not_on_first_install(monkeypatch):
    monkeypatch.setattr(selfcheck, "uptime_seconds", lambda: 700.0)
    state: dict = {}
    assert selfcheck.boot_lines({"rabbitmq": None}, state, "boot-1", in_grace=False) == []  # first install
    assert selfcheck.boot_lines({"rabbitmq": None}, state, "boot-1", in_grace=False) == []
    assert selfcheck.boot_lines({"rabbitmq": "không trả lời ping"}, state, "boot-2", in_grace=True) == []
    lines = selfcheck.boot_lines({"rabbitmq": "không trả lời ping"}, state, "boot-2", in_grace=False)
    assert lines[0].startswith("🔄 Máy Ceph AI đã khởi động lại") and "• rabbitmq: không trả lời ping" in lines[0]
    assert selfcheck.boot_lines({"rabbitmq": None}, state, "boot-2", in_grace=False) == []


def test_containers_missing_stopped_or_unhealthy(monkeypatch):
    listed = [
        {"Names": ["rabbitmq"], "State": "running", "Status": "Up 2 months"},
        {"Names": ["ceph-ai_worker_1"], "State": "running", "Status": "Up 1 hour (unhealthy)"},
        {"Names": ["ceph-ai_watcher_1"], "State": "exited", "Status": "Exited (1)"},
    ]
    monkeypatch.setattr(selfcheck, "_podman", lambda *args: SimpleNamespace(stdout=json.dumps(listed), returncode=0))

    results = selfcheck.check_containers()

    assert results["container:rabbitmq"] is None
    assert results["container:ceph-ai_worker_1"] == "unhealthy"
    assert results["container:ceph-ai_watcher_1"] == "không chạy (exited)"
    assert results["container:ceph-ai_dashboard-web_1"] == "không tồn tại"


def test_podman_timeout_is_a_problem_not_a_crash(monkeypatch):
    def slow(*args):
        raise subprocess.TimeoutExpired(cmd="podman", timeout=20)

    monkeypatch.setattr(selfcheck, "_podman", slow)

    assert selfcheck.check_rabbitmq() == {"rabbitmq": "không trả lời (TimeoutExpired)"}
    assert "không đọc được podman" in selfcheck.check_containers()["containers"]


def test_heartbeats_stale_or_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(selfcheck, "RUNTIME_DIR", tmp_path)
    fresh = datetime.fromtimestamp(T0 - 10, tz=timezone.utc).isoformat()
    old = datetime.fromtimestamp(T0 - 600, tz=timezone.utc).isoformat()
    (tmp_path / "watcher.json").write_text(json.dumps({"updated_at": fresh}))
    (tmp_path / "worker.json").write_text(json.dumps({"updated_at": old}))
    (tmp_path / "telegram-ai.json").write_text("{not json")

    results = selfcheck.check_heartbeats(T0)

    assert results["heartbeat:watcher"] is None
    assert results["heartbeat:worker"] == "heartbeat cũ 600 giây"
    assert results["heartbeat:telegram-ai"] == "không có heartbeat"
    assert results["heartbeat:remediation-watcher"] == "không có heartbeat"


def test_env_parsing_and_database_check_without_credentials(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text('# comment\nDATABASE_URL="postgresql://u:p@127.0.0.1:1/db"\nTELEGRAM_NODE_CHAT_ID=42\n')

    env = selfcheck.read_env(env_file)

    assert env["DATABASE_URL"] == "postgresql://u:p@127.0.0.1:1/db" and env["TELEGRAM_NODE_CHAT_ID"] == "42"
    problem = selfcheck.check_database(env)["database"]
    assert problem.startswith("không kết nối được 127.0.0.1:1") and "p@" not in problem
    assert selfcheck.check_database({"DATABASE_URL": "sqlite:///x.db"}) == {"database": None}


def test_a_failed_send_keeps_the_alert_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(selfcheck, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(selfcheck, "read_env", lambda: {})
    monkeypatch.setattr(selfcheck, "load_state", lambda: {"boot_reported": "b", "checks": {
        "rabbitmq": {"failures": 1, "alerted_at": None, "since": T0}}})
    monkeypatch.setattr(selfcheck, "boot_id", lambda: "b")
    monkeypatch.setattr(selfcheck, "uptime_seconds", lambda: 9999.0)
    monkeypatch.setattr(selfcheck, "run_checks", lambda env, now: {"rabbitmq": "không trả lời ping"})
    saved = {}
    monkeypatch.setattr(selfcheck, "save_state", lambda state: saved.update(state))

    assert selfcheck.main() == 0  # no bot configured -> send fails

    assert saved["checks"]["rabbitmq"]["alerted_at"] is None  # retried next run


def test_units_run_independently_of_the_stack_every_minute():
    systemd = ROOT / "scripts" / "deploy" / "systemd"
    service = (systemd / "ceph-ai-selfcheck.service").read_text(encoding="utf-8")
    timer = (systemd / "ceph-ai-selfcheck.timer").read_text(encoding="utf-8")
    assert "ExecStart=/usr/bin/python3.11 /root/ceph-ai/scripts/selfcheck/ceph_ai_selfcheck.py" in service
    directives = [line for line in service.splitlines() if line and not line.startswith(("#", "["))]
    assert not any("ceph-ai-containers" in line for line in directives)  # runs even when the stack is down
    assert "OnUnitActiveSec=60" in timer and "WantedBy=timers.target" in timer
    deploy = (ROOT / "scripts" / "deploy" / "restart_container_stack.sh").read_text(encoding="utf-8")
    assert "systemctl enable --now ceph-ai-selfcheck.timer" in deploy


def test_the_script_uses_only_the_standard_library():
    source = (ROOT / "scripts" / "selfcheck" / "ceph_ai_selfcheck.py").read_text(encoding="utf-8")
    imports = {line.split()[1].split(".")[0] for line in source.splitlines() if line.startswith(("import ", "from "))}
    assert imports <= {"__future__", "http", "json", "os", "shutil", "socket", "subprocess", "sys", "time", "datetime", "pathlib", "urllib"}


def _unhealthy_runs(state, name, runs, start=T0):
    lines = []
    for run in range(runs):
        now = start + 60 * run
        results = {f"container:{name}": "unhealthy"}
        selfcheck.evaluate(results, state, now, in_grace=False)
        lines += selfcheck.heal(results, state, now, in_grace=False)
    return lines


def test_an_unhealthy_container_is_restarted_after_three_runs_within_budget(monkeypatch):
    restarted = []
    monkeypatch.setattr(selfcheck, "_podman", lambda *args: restarted.append(args) or SimpleNamespace(returncode=0))
    state: dict = {}

    lines = _unhealthy_runs(state, "ceph-ai_worker_1", 3)

    assert restarted == [("restart", "-t", "30", "ceph-ai_worker_1")]
    assert lines == ["🔧 ceph-ai_worker_1: unhealthy 3 phút → đã restart (lần 1/3 trong 6 giờ)"]

    more = _unhealthy_runs(state, "ceph-ai_worker_1", 9, start=T0 + 180)
    assert len(restarted) == 3
    assert more[-1] == "⛔ ceph-ai_worker_1: đã tự restart 3 lần trong 6 giờ, không tự restart nữa"
    assert _unhealthy_runs(state, "ceph-ai_worker_1", 3, start=T0 + 900) == []  # limit reported once

    _unhealthy_runs(state, "ceph-ai_worker_1", 3, start=T0 + selfcheck.RESTART_WINDOW_SECONDS + 600)
    assert len(restarted) == 4  # the window moved on


def test_full_executor_and_rabbitmq_are_never_restarted_automatically(monkeypatch):
    monkeypatch.setattr(selfcheck, "_podman", lambda *args: pytest.fail("must not restart"))
    state: dict = {}

    assert _unhealthy_runs(state, "ceph-ai_full-executor_1", 5) == []
    assert selfcheck.heal({"container:ceph-ai_worker_1": "unhealthy"}, {}, T0, in_grace=True) == []
    assert selfcheck.heal({"container:ceph-ai_worker_1": "không chạy (exited)"},
                          {"checks": {"container:ceph-ai_worker_1": {"failures": 9}}}, T0, in_grace=False) == []


def test_telegram_outbox_counts_stuck_and_dead_messages(monkeypatch):
    replies = iter([
        json.dumps({"pending_old": 0, "dead_1h": {}}),
        json.dumps({"pending_old": 2, "dead_1h": {"log-intelligence": 3, "rgw": 1}}),
    ])
    monkeypatch.setattr(selfcheck, "_podman", lambda *args: SimpleNamespace(stdout="noise\n" + next(replies), returncode=0))

    assert selfcheck.check_telegram_outbox() == {"telegram_outbox": None}
    assert selfcheck.check_telegram_outbox() == {"telegram_outbox":
        "2 tin chờ gửi quá 15 phút; 4 tin gửi thất bại hẳn (DEAD) trong 1 giờ: log-intelligence 3, rgw 1"}


def test_telegram_outbox_check_never_reads_message_contents():
    query = selfcheck._OUTBOX_QUERY
    assert "last_error" not in query and "payload" not in query
    assert "T.category" in query and ".count()" in query


def test_an_unreachable_worker_does_not_double_report(monkeypatch):
    monkeypatch.setattr(selfcheck, "_podman", lambda *args: SimpleNamespace(stdout="", returncode=125))

    assert selfcheck.check_telegram_outbox() == {"telegram_outbox": None}

"""Read model for the "Luồng AI" tab of the Stream page.

One picture of how the AI works end to end — detection, incident, evidence,
diagnosis, policy, approval/autopilot, execution, verification and the
learning loop (case memory, trust, online learning, Failure Lab) — with the
real numbers of the last 24 hours for every step. Read-only: database counts
and the Failure Lab report directory, never a Ceph command.

A step is ``warn``/``error`` when its own numbers show a problem (evidence
never collected while incidents arrive, executions failing, ...), so the
picture points at the step that needs attention.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func

from config.settings import settings
from shared import db
from shared.models import (
    Action, AuditEntry, Incident, IncidentEvidence, LogFinding, OnlineLearnerLabel, PlaybookStat, RemediationCase,
)
from shared.time import utc_now

SCHEMA = "ceph-ai.ai-flow.v1"
WINDOW = timedelta(hours=24)
FAILURE_LAB_DIR = Path("/var/lib/ceph-ai/failure-lab")
OK, WARN, ERROR, UNKNOWN = "ok", "warn", "error", "unknown"

GROUPS = [
    {"id": "detect", "title": "PHÁT HIỆN"},
    {"id": "incident", "title": "SỰ CỐ"},
    {"id": "diagnose", "title": "CHẨN ĐOÁN"},
    {"id": "decide", "title": "QUYẾT ĐỊNH"},
    {"id": "act", "title": "THỰC THI · XÁC MINH"},
    {"id": "learn", "title": "HỌC"},
]
# Incident codes raised by Ceph AI's own detectors rather than `ceph health`.
_TELEMETRY_PREFIXES = ("NODE_UNREACHABLE", "OSD_LATENCY_HIGH", "NODE_RESOURCE_HIGH", "CRUSH_SKEW", "CEPH_HEALTH_UNAVAILABLE")
_OPERATOR_PREFIXES = ("CLUSTER_", "RBD_", "VOLUME_", "BACKUP_", "NODE_OS_")
_OPEN_INCIDENT = ("NEW", "DIAGNOSING", "PENDING_APPROVAL", "APPROVED", "EXECUTING", "GRACE_PENDING", "VERIFYING")


def _source(code: str) -> str:
    base = (code or "").split(":", 1)[0]
    if base == "LOG_ANOMALY":
        return "log"
    if base.startswith(_TELEMETRY_PREFIXES):
        return "telemetry"
    if base.startswith(_OPERATOR_PREFIXES):
        return "operator"
    return "health"


def _node(node_id: str, group: str, kind: str, title: str, subtitle: str, status: str,
          facts: list[str], href: str | None = None) -> dict[str, Any]:
    return {"id": node_id, "group": group, "kind": kind, "title": title, "subtitle": subtitle,
            "status": status, "facts": facts, "checks": [], "href": href}


def _counts(session, since: datetime) -> dict[str, Any]:
    incidents = session.query(Incident.ceph_code, Incident.status, Incident.diagnosis_text).filter(
        Incident.created_at > since).all()
    actions = session.query(Action.classification, Action.status).filter(Action.created_at > since).all()
    events = Counter(row[0] for row in session.query(AuditEntry.event_type).filter(AuditEntry.created_at > since))
    cases_24h = Counter(row[0] for row in session.query(RemediationCase.outcome).filter(RemediationCase.created_at > since))
    return {
        "incidents": incidents,
        "sources": Counter(_source(code) for code, _status, _diagnosis in incidents),
        "diagnosed": sum(1 for _code, _status, diagnosis in incidents if (diagnosis or "").strip()),
        "failed_incidents": sum(1 for _code, status, _diagnosis in incidents if status == "FAILED"),
        "open_incidents": session.query(Incident).filter(Incident.status.in_(_OPEN_INCIDENT)).count(),
        "evidence": session.query(IncidentEvidence).filter(IncidentEvidence.created_at > since).count(),
        "actions": Counter(actions),
        "pending_now": session.query(Action).filter(Action.status == "PENDING_APPROVAL").count(),
        "events": events,
        "cases_24h": cases_24h,
        "verified_total": session.query(RemediationCase).filter(RemediationCase.outcome == "VERIFIED_SUCCESS").count(),
        "cases_total": session.query(func.count(RemediationCase.id)).scalar() or 0,
        "open_log_findings": session.query(LogFinding).filter(LogFinding.status == "OPEN").count(),
        "labels_total": session.query(func.count(OnlineLearnerLabel.id)).scalar() or 0,
        "labels_24h": session.query(OnlineLearnerLabel).filter(OnlineLearnerLabel.observed_at > since).count(),
        "playbooks": session.query(PlaybookStat.maturity_level, PlaybookStat.auto_disabled_reason).all(),
    }


def _latest_failure_lab(directory: Path) -> dict[str, Any] | None:
    try:
        reports = sorted(directory.glob("*.json"), key=lambda path: path.stat().st_mtime)
        report = json.loads(reports[-1].read_text(encoding="utf-8")) if reports else None
    except (OSError, ValueError):
        return None
    if not isinstance(report, dict):
        return None
    return {"campaign_id": report.get("campaign_id"), "passed": report.get("passed_count", 0),
            "runs": report.get("run_count", 0), "skipped": len(report.get("skipped") or [])}


def _detection_nodes(c: dict[str, Any]) -> list[dict[str, Any]]:
    sources = c["sources"]
    return [
        _node("detect_health", "detect", "detect", "Health check Ceph", f"{sources['health']} incident / 24h", OK,
              ["Watcher đọc `ceph health detail` mỗi chu kỳ", "Mã HEALTH_WARN/ERR thành incident"], "/alerts"),
        _node("detect_telemetry", "detect", "detect", "Telemetry & dự báo", f"{sources['telemetry']} incident / 24h", OK,
              ["Node không phản hồi, độ trễ OSD, tài nguyên, lệch CRUSH", "Dựa trên baseline online learning"],
              "/nodes"),
        _node("detect_log", "detect", "detect", "Log Intelligence", f"{sources['log']} incident · {c['open_log_findings']} finding mở",
              WARN if c["open_log_findings"] > 50 else OK, ["Mẫu log bất thường theo template", "Finding mở được nhắc lại định kỳ"],
              "/log-intelligence"),
        _node("detect_operator", "detect", "detect", "Thao tác operator", f"{sources['operator']} yêu cầu / 24h", OK,
              ["Deploy/xóa/nâng cấp cụm, volume, backup", "Đi cùng đường duyệt như sự cố"], "/deploy-cluster"),
    ]


def _incident_nodes(c: dict[str, Any]) -> list[dict[str, Any]]:
    recurred = c["events"].get("incident_recurred", 0)
    return [
        _node("incidents", "incident", "incident", "Incident", f"{len(c['incidents'])} mới / 24h · {c['open_incidents']} đang mở",
              WARN if recurred else OK, [f"Tái phát: {recurred}", f"Synthetic bị chặn thực thi: {c['events'].get('synthetic_execution_blocked', 0)}"],
              "/alerts"),
    ]


def _evidence_node(c: dict[str, Any], total: int) -> dict[str, Any]:
    facts = ["Đọc health/OSD/log theo runbook trước khi hỏi LLM", "Chỉ lệnh chỉ-đọc"]
    if not settings.investigation_enabled:
        # Off by design until enabled per canary cluster; not a fault, but say so.
        return _node("evidence", "diagnose", "evidence", "Thu bằng chứng (runbook)", "TẮT", UNKNOWN,
                     facts + ["INVESTIGATION_ENABLED=false: chẩn đoán đang dựa trên dữ liệu thô"])
    canary = settings.investigation_cluster_ids.strip()
    facts.append(f"Chỉ cụm canary: {canary}" if canary else "Mọi cụm")
    if total > 0 and c["evidence"] == 0:
        facts.append("Không thu bằng chứng nào dù có incident: xem log Worker 'evidence_gate: no evidence'")
        return _node("evidence", "diagnose", "evidence", "Thu bằng chứng (runbook)", "0 lần thu / 24h", WARN, facts)
    return _node("evidence", "diagnose", "evidence", "Thu bằng chứng (runbook)", f"{c['evidence']} lần thu / 24h",
                 OK, facts)


def _diagnosis_nodes(c: dict[str, Any]) -> list[dict[str, Any]]:
    total = len(c["incidents"])
    low_confidence = c["events"].get("proposal_blocked_by_low_confidence", 0)
    return [
        _evidence_node(c, total),
        _node("diagnosis", "diagnose", "llm", "Chẩn đoán AI", f"{c['diagnosed']}/{total} incident có chẩn đoán",
              ERROR if c["failed_incidents"] else (WARN if low_confidence else OK),
              [f"Chẩn đoán thất bại: {c['failed_incidents']}", f"Bị chặn vì độ tin cậy thấp: {low_confidence}",
               "Triage tất định trước, LLM sau"], "/ai-tasks"),
    ]


def _decision_nodes(c: dict[str, Any]) -> list[dict[str, Any]]:
    events, actions = c["events"], c["actions"]
    blocked = sum(count for name, count in events.items() if "blocked" in name and not name.startswith("synthetic"))
    approved = events.get("risky_action_approved", 0)
    rejected = sum(count for (_cls, status), count in actions.items() if status == "REJECTED")
    auto = sum(count for (cls, status), count in actions.items() if cls == "SAFE" and status == "AUTO_EXECUTED")
    return [
        _node("policy", "decide", "policy", "Policy · Preflight", f"{blocked} lần chặn / 24h", OK,
              [f"Preflight chặn: {events.get('proposal_blocked_by_preflight', 0)}",
               f"Autopilot bị cổng chặn: {sum(count for name, count in events.items() if name.startswith('autopilot_') and 'blocked' in name)}",
               "Chỉ action_id trong danh mục đã duyệt"], "/capability-matrix"),
        _node("approval", "decide", "approval", "Duyệt (RISKY)", f"{c['pending_now']} chờ duyệt · {approved} duyệt / 24h",
              WARN if c["pending_now"] > 20 else OK, [f"Từ chối / tự hủy: {rejected}", "Operator duyệt qua Dashboard/Telegram"],
              "/alerts"),
        _node("autopilot", "decide", "autopilot", "Autopilot (SAFE)", f"{auto} tự chạy / 24h", OK,
              ["Chỉ SAFE, qua cổng vận hành và hợp đồng playbook", "Lab có đếm ngược để hủy"]),
    ]


def _action_nodes(c: dict[str, Any]) -> list[dict[str, Any]]:
    actions, cases = c["actions"], c["cases_24h"]
    executed = sum(count for (_cls, status), count in actions.items() if status in {"EXECUTED", "AUTO_EXECUTED"})
    failed = sum(count for (_cls, status), count in actions.items() if status == "FAILED")
    verified_ok, verified_bad = cases.get("VERIFIED_SUCCESS", 0), cases.get("VERIFIED_FAILED", 0) + cases.get("INCONCLUSIVE", 0)
    return [
        _node("execution", "act", "execute", "Thực thi", f"{executed} chạy · {failed} lỗi / 24h",
              ERROR if failed and failed >= executed else (WARN if failed else OK),
              ["Executor riêng, khóa mutation, lệnh dựng sẵn", "Không chạy lệnh tự do của AI"]),
        _node("verification", "act", "verify", "Xác minh", f"{verified_ok} thành công · {verified_bad} không đạt / 24h",
              WARN if verified_bad else OK, [f"Chờ xác minh: {cases.get('EXECUTED_PENDING_VERIFY', 0)}",
                                             "So telemetry trước/sau, theo dõi tái phát 1h/24h/7d"]),
    ]


def _learning_nodes(c: dict[str, Any], failure_lab: dict[str, Any] | None) -> list[dict[str, Any]]:
    playbooks = c["playbooks"]
    l3 = sum(1 for level, _reason in playbooks if level == "L3")
    disabled = sum(1 for _level, reason in playbooks if reason)
    lab_subtitle = (f"lượt gần nhất {failure_lab['passed']}/{failure_lab['runs']} đạt" if failure_lab
                    else "chưa chạy chiến dịch nào")
    lab_status = UNKNOWN if not failure_lab else (OK if failure_lab["passed"] == failure_lab["runs"] else WARN)
    return [
        _node("case_memory", "learn", "memory", "Case Memory", f"{c['verified_total']} case đã xác minh / {c['cases_total']}",
              WARN if c["verified_total"] < 10 else OK,
              ["Case đã xác minh được đưa lại vào chẩn đoán", "Case lab/synthetic không nâng trust production"], "/ai-learning"),
        _node("trust", "learn", "trust", "Trust Engine", f"{len(playbooks)} playbook · {l3} ở L3",
              WARN if disabled else OK, [f"Bị tự tắt: {disabled}", "Chỉ đề xuất nâng cấp, operator duyệt"], "/ai-learning"),
        _node("online_learning", "learn", "online", "Online learning", f"{c['labels_24h']} nhãn / 24h · {c['labels_total']} tổng",
              OK if c["labels_24h"] else WARN, ["Baseline & dự báo cho telemetry", "Chạy shadow/canary trước khi dùng"],
              "/ai-learning"),
        _node("failure_lab", "learn", "lab", "Failure Lab", lab_subtitle, lab_status,
              [f"Chiến dịch: {failure_lab['campaign_id']}" if failure_lab else "Replay sự cố có đáp án trên CS-LAB",
               "Chấm chẩn đoán + đề xuất của AI"]),
    ]


_EDGES = [
    ("detect_health", "incidents", "mã health", "data"), ("detect_telemetry", "incidents", "tín hiệu", "data"),
    ("detect_log", "incidents", "finding", "data"), ("detect_operator", "incidents", "yêu cầu", "data"),
    ("incidents", "evidence", "runbook", "control"), ("evidence", "diagnosis", "bằng chứng", "data"),
    ("diagnosis", "policy", "action_id đề xuất", "control"), ("policy", "approval", "RISKY", "control"),
    ("policy", "autopilot", "SAFE", "control"), ("approval", "execution", "đã duyệt", "control"),
    ("autopilot", "execution", "tự chạy", "control"), ("execution", "verification", "kết quả", "data"),
    ("verification", "case_memory", "case", "data"), ("case_memory", "trust", "thống kê", "data"),
    ("trust", "policy", "trust score", "network"), ("case_memory", "diagnosis", "case tương tự", "network"),
    ("online_learning", "detect_telemetry", "baseline", "network"), ("failure_lab", "diagnosis", "chấm điểm", "management"),
]


def build(*, now: datetime | None = None, session_factory=None, failure_lab_dir: Path = FAILURE_LAB_DIR) -> dict[str, Any]:
    now = now or utc_now()
    with (session_factory or db.SessionLocal)() as session:
        counts = _counts(session, now - WINDOW)
    nodes = (_detection_nodes(counts) + _incident_nodes(counts) + _diagnosis_nodes(counts) + _decision_nodes(counts)
             + _action_nodes(counts) + _learning_nodes(counts, _latest_failure_lab(failure_lab_dir)))
    node_ids = {node["id"] for node in nodes}
    return {
        "schema": SCHEMA,
        "generated_at": now.isoformat(),
        "window_hours": int(WINDOW.total_seconds() // 3600),
        "summary": {
            "incidents": len(counts["incidents"]),
            "diagnosed": counts["diagnosed"],
            "evidence": counts["evidence"],
            "pending_approval": counts["pending_now"],
            "verified_total": counts["verified_total"],
            "problems": sum(1 for node in nodes if node["status"] in {WARN, ERROR}),
        },
        "groups": GROUPS,
        "nodes": nodes,
        "edges": [{"from": a, "to": b, "label": label, "kind": kind} for a, b, label, kind in _EDGES
                  if {a, b} <= node_ids],
    }

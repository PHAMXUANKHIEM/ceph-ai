"""Epic 10 Story 10.7: Report Generator. Pure, SSH-free functions that turn
Story 10.6's in-memory run-state dict + the TEST_CASES_BY_ID registry into
the Markdown/Excel/Copy-Summary exports. Deliberately takes plain dicts in
(never dashboard.routes.test_runner's own _RunState dataclass, never a
FastAPI/DB session object) so this module stays importable and unit-testable
without dashboard/ -- worker/ must not import from dashboard/ (see
shared/test_runner_baselines.py's docstring for the same rule applied
elsewhere in Epic 10).

Source of truth for the exact structure reproduced here:
docs/ceph-upgrade-test-cases.md, Section 4 ("Bieu mau ghi ket qua") and
Section 5 ("Tieu chi ket thuc"). Do not reword the Section 5 checklist items.

Known correction (see Story 10.7 Dev Notes for the full derivation): the
source document's own Section 4 aggregate table claims POST=33/Tong=63, but
the document's actual Section 3 content has 41 real POST test cases (already
independently confirmed by Story 10.4). This module reports the REAL counts
(POST=41, Tong=71) and discloses the correction in the report text rather
than silently reproducing the document's stale numbers.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from openpyxl import Workbook

__all__ = [
    "ReportRow",
    "NOT_IMPLEMENTED_DOCUMENT_IDS",
    "DOCUMENT_ID_OVERRIDES",
    "expand_document_ids",
    "map_status_to_pass_fail",
    "build_report_rows",
    "build_aggregate_table",
    "build_run013_osd_table",
    "build_exit_criteria_checklist",
    "build_markdown_report",
    "build_excel_workbook",
    "build_copy_summary_text",
]

# Only the exceptions need listing -- every other engine TestCase.id maps to
# exactly one document id (itself). TC-PERF-001-003 is the one class in the
# whole 67-class registry whose single id bundles 3 document ids (see
# worker/executor/test_runner/group_d.py::TcPerf001To003RbdPerformance).
DOCUMENT_ID_OVERRIDES: dict[str, list[str]] = {
    "TC-PERF-001-003": ["TC-PERF-001", "TC-PERF-002", "TC-PERF-003"],
}

# TC-RUN-002/003: documented in docs/ceph-upgrade-test-cases.md's Section 4
# summary table (RUN=13) but have no content anywhere in the document body
# and no TestCase implementation (Story 10.3's disclosed, still-open gap).
# Listed here so the report shows them as real rows -- "not implemented",
# not silently missing.
NOT_IMPLEMENTED_DOCUMENT_IDS: list[str] = ["TC-RUN-002", "TC-RUN-003"]

_GROUP_ORDER = {"RUN": 0, "POST": 1, "COMPAT": 2, "PERF": 3}

_STATUS_TO_PASS_FAIL = {
    "pass": "Pass",
    "fail": "Fail",
    "error": "Blocked",
    "skip": "N/A",
}


_KNOWN_DOC_ID_PREFIXES = (("TC-RUN-", "RUN"), ("TC-POST-", "POST"), ("TC-COMPAT-", "COMPAT"), ("TC-PERF-", "PERF"))


def _group_for_doc_id(doc_id: str) -> str:
    for prefix, group in _KNOWN_DOC_ID_PREFIXES:
        if doc_id.startswith(prefix):
            return group
    raise ValueError(f"khong nhan dien duoc nhom tu document id: {doc_id!r}")


def _is_report_scoped_id(doc_id: str) -> bool:
    """This report is fixed to docs/ceph-upgrade-test-cases.md's exact
    71-row structure (RUN/POST/COMPAT/PERF only) -- Group E (S3, its own
    separate docs/s3-upgrade-test-cases.md, `TC-S3-*` ids) and any future
    group intentionally do not participate in THIS report rather than
    crashing it. A future Group-E-specific report is out of scope here."""
    return doc_id.startswith(("TC-RUN-", "TC-POST-", "TC-COMPAT-", "TC-PERF-"))


def _sort_key(doc_id: str) -> tuple[int, int]:
    group = _group_for_doc_id(doc_id)
    tail = doc_id.rsplit("-", 1)[-1]
    try:
        num = int(tail)
    except ValueError:
        num = 0
    return (_GROUP_ORDER[group], num)


def expand_document_ids(engine_id: str) -> list[str]:
    return DOCUMENT_ID_OVERRIDES.get(engine_id, [engine_id])


def map_status_to_pass_fail(status: Optional[str], overridden: bool) -> str:
    """Background point 5 of Story 10.7: the aggregate table only has 4
    buckets (Pass/Fail/Blocked/N/A) -- there is no 5th bucket for
    running/pending/never-run, which map to a blank cell (matches the source
    document's own blank-template convention for a test not yet closed out).
    `overridden` doesn't change the mapping itself (an override always
    writes status="pass"/"fail" already, per Story 10.6's AC6) -- it only
    affects the Ghi chu column, handled separately in build_report_rows().
    """
    if status is None:
        return ""
    return _STATUS_TO_PASS_FAIL.get(status, "")


@dataclass
class ReportRow:
    document_id: str
    group: str
    priority: Optional[str]
    ngay_thuc_hien: str = ""
    nguoi_thuc_hien: str = ""
    ket_qua_thuc_te: str = ""
    pass_fail: str = ""
    defect_id: str = ""
    ghi_chu: str = ""


def _date_part(iso_value: Optional[str]) -> str:
    if not iso_value:
        return ""
    try:
        return datetime.fromisoformat(iso_value).date().isoformat()
    except ValueError:
        return iso_value[:10]


def _ket_qua_thuc_te(run_state: Optional[dict]) -> str:
    if run_state is None:
        return "Chưa chạy"
    criteria = run_state.get("criteria") or []
    if criteria:
        n_pass = sum(1 for c in criteria if c.get("passed") is True)
        summary = f"{n_pass}/{len(criteria)} tiêu chí đạt"
    else:
        summary = "Chưa có tiêu chí"
    notes = (run_state.get("notes") or "").strip()
    return f"{summary}; {notes}" if notes else summary


def build_report_rows(
    run_states: dict[str, dict], test_cases_by_id: dict[str, Any], username: str
) -> list[ReportRow]:
    """One row per real document test id (Story 10.7 AC1's 71 rows), built
    from the CALLER's own run_states/test_cases_by_id dicts (explicit
    params, not a module global this file reads on its own) -- same "two
    independent bindings" footgun Story 10.4/10.6 both hit once already for
    shared/test_runner_baselines.py and registry.py::filter_selected(), not
    repeated here.
    """
    rows: dict[str, ReportRow] = {}

    for engine_id, test_case in test_cases_by_id.items():
        if not _is_report_scoped_id(engine_id):
            continue
        run_state = run_states.get(engine_id)
        pass_fail = map_status_to_pass_fail(
            run_state.get("status") if run_state else None,
            run_state.get("overridden", False) if run_state else False,
        )
        ghi_chu = ""
        if run_state and run_state.get("overridden"):
            note = (run_state.get("override_note") or "").strip()
            ghi_chu = f"[Override] {note}" if note else "[Override]"
        row_template = ReportRow(
            document_id="",
            group="",
            priority=getattr(test_case.priority, "value", None),
            ngay_thuc_hien=_date_part(run_state.get("finished_at") or run_state.get("started_at")) if run_state else "",
            nguoi_thuc_hien=username,
            ket_qua_thuc_te=_ket_qua_thuc_te(run_state),
            pass_fail=pass_fail,
            defect_id="",
            ghi_chu=ghi_chu,
        )
        for doc_id in expand_document_ids(engine_id):
            rows[doc_id] = ReportRow(
                document_id=doc_id,
                group=_group_for_doc_id(doc_id),
                priority=row_template.priority,
                ngay_thuc_hien=row_template.ngay_thuc_hien,
                nguoi_thuc_hien=row_template.nguoi_thuc_hien,
                ket_qua_thuc_te=row_template.ket_qua_thuc_te,
                pass_fail=row_template.pass_fail,
                defect_id=row_template.defect_id,
                ghi_chu=row_template.ghi_chu,
            )

    for doc_id in NOT_IMPLEMENTED_DOCUMENT_IDS:
        rows[doc_id] = ReportRow(
            document_id=doc_id,
            group=_group_for_doc_id(doc_id),
            priority=None,
            nguoi_thuc_hien=username,
            ket_qua_thuc_te="Không có test case tương ứng trong engine",
            ghi_chu="Không có nội dung test case trong tài liệu nguồn (gap đã biết, xem Story 10.3)",
        )

    return [rows[doc_id] for doc_id in sorted(rows, key=_sort_key)]


def build_aggregate_table(rows: list[ReportRow]) -> list[dict]:
    """Real counts, not the source document's own stale Section-4 numbers
    (see module docstring) -- RUN=13 (11 implemented + 2 not-implemented),
    POST=41 (not 33), COMPAT=8, PERF=9, Tong=71 (not 63).
    """
    groups = ["RUN", "POST", "COMPAT", "PERF"]
    table = []
    totals = {"tong_so_tc": 0, "pass": 0, "fail": 0, "blocked": 0, "na": 0}
    for group in groups:
        group_rows = [r for r in rows if r.group == group]
        counts = {
            "tong_so_tc": len(group_rows),
            "pass": sum(1 for r in group_rows if r.pass_fail == "Pass"),
            "fail": sum(1 for r in group_rows if r.pass_fail == "Fail"),
            "blocked": sum(1 for r in group_rows if r.pass_fail == "Blocked"),
            "na": sum(1 for r in group_rows if r.pass_fail == "N/A"),
        }
        for key in totals:
            totals[key] += counts[key]
        ti_le = f"{counts['pass'] / counts['tong_so_tc'] * 100:.1f}%" if counts["tong_so_tc"] else "-"
        table.append({"group": group, **counts, "ti_le_pass": ti_le})
    ti_le_total = f"{totals['pass'] / totals['tong_so_tc'] * 100:.1f}%" if totals["tong_so_tc"] else "-"
    table.append({"group": "Tổng", **totals, "ti_le_pass": ti_le_total})
    return table


def build_run013_osd_table(run_states: dict[str, dict]) -> Optional[list[dict]]:
    """Story 10.7 Dev Notes point 4: TC-RUN-013's background_state (Story
    10.6 keeps it in-process only, never returned as-is to the browser) has
    a plain-JSON-safe `completed` list once at least one OSD has finished --
    this function reads that structured field for report rendering, which is
    a different thing from Story 10.6's "never serialize background_state to
    the client" rule (that rule is about the frontend JSON API, not backend
    report code). Returns None (no sub-table, not an error) if the test
    never ran via .start()/.poll() in this process lifetime.
    """
    run_state = run_states.get("TC-RUN-013")
    if not run_state:
        return None
    background_state = run_state.get("background_state")
    if not background_state:
        return None
    completed = background_state.get("completed")
    if not completed:
        return None
    return [
        {
            "osd_id": c.get("osd_id"),
            "seconds": round(c["seconds"], 1) if c.get("seconds") is not None else None,
            "exit_code": c.get("exit_code"),
            "over_estimate": c.get("over_estimate"),
        }
        for c in completed
    ]


_EXIT_CRITERIA_ITEMS = [
    "100% test P1 PASS, ≥ 95% test P2 PASS.",
    "0 defect Critical/Blocker còn mở.",
    "100% checksum khớp baseline trên RBD/CephFS/RGW.",
    "`ceph versions` cho thấy toàn bộ daemon ở 16.2.15.",
    "`ceph -s` = `HEALTH_OK` (hoặc chỉ còn cảnh báo đã giải thích & chấp nhận).",
    "Suy giảm hiệu năng sau nâng cấp ≤ 10% so với baseline.",
    "Soak test 72 giờ không phát sinh crash/PG lỗi/rò rỉ bộ nhớ.",
    "Deep-scrub xác nhận 0 PG inconsistent — bằng chứng cuối cùng rằng convert OMAP 2 lớp không làm hỏng dữ liệu.",
    "TC-RUN-013 (convert OMAP 2 lớp) đã hoàn tất trên 100% OSD, không có OSD nào phải chạy lại do lỗi.",
]


def build_exit_criteria_checklist(rows: list[ReportRow]) -> list[tuple[str, bool, str]]:
    """The 9 items from docs/ceph-upgrade-test-cases.md Section 5, verbatim
    wording, each (item_text, checked, note) -- see Story 10.7 Dev Notes'
    mapping table for the source of each rule. Items 2 and 6 can never be
    auto-checked (no defect tracker in this project; every PERF criterion is
    passed=None by Story 10.5 design, needing a manual baseline compare) --
    always False with an explanatory note, not silently omitted.
    """
    by_id = {r.document_id: r for r in rows}

    def _passed(doc_id: str) -> bool:
        row = by_id.get(doc_id)
        return row is not None and row.pass_fail == "Pass"

    p1_rows = [r for r in rows if r.priority == "P1"]
    p2_rows = [r for r in rows if r.priority == "P2"]
    p1_all_pass = bool(p1_rows) and all(r.pass_fail == "Pass" for r in p1_rows)
    p2_pass_ratio = (sum(1 for r in p2_rows if r.pass_fail == "Pass") / len(p2_rows)) if p2_rows else 1.0
    item1_checked = p1_all_pass and p2_pass_ratio >= 0.95

    results = [
        (_EXIT_CRITERIA_ITEMS[0], item1_checked, "" if item1_checked else "P1/P2 chưa đạt ngưỡng"),
        (_EXIT_CRITERIA_ITEMS[1], False, "cần xác nhận thủ công (không có hệ thống theo dõi defect)"),
        (
            _EXIT_CRITERIA_ITEMS[2],
            all(_passed(i) for i in ("TC-POST-010", "TC-POST-011", "TC-POST-013", "TC-POST-015")),
            "",
        ),
        (_EXIT_CRITERIA_ITEMS[3], _passed("TC-POST-001"), ""),
        (_EXIT_CRITERIA_ITEMS[4], _passed("TC-POST-002"), ""),
        (_EXIT_CRITERIA_ITEMS[5], False, "cần đối chiếu thủ công với baseline (xem TC-PERF-*)"),
        (_EXIT_CRITERIA_ITEMS[6], _passed("TC-PERF-009"), ""),
        (_EXIT_CRITERIA_ITEMS[7], _passed("TC-POST-017"), ""),
        (_EXIT_CRITERIA_ITEMS[8], _passed("TC-RUN-013"), ""),
    ]
    return results


_ROW_HEADERS = ["Test Case ID", "Ngày thực hiện", "Người thực hiện", "Kết quả thực tế", "Pass/Fail", "Defect ID", "Ghi chú"]
_AGGREGATE_HEADERS = ["Nhóm", "Tổng số TC", "Pass", "Fail", "Blocked", "N/A", "Tỉ lệ Pass"]


def _markdown_row_table(rows: list[ReportRow]) -> str:
    lines = ["| " + " | ".join(_ROW_HEADERS) + " |", "|" + "---|" * len(_ROW_HEADERS)]
    for r in rows:
        lines.append(
            "| "
            + " | ".join(
                [r.document_id, r.ngay_thuc_hien, r.nguoi_thuc_hien, r.ket_qua_thuc_te, r.pass_fail, r.defect_id, r.ghi_chu]
            )
            + " |"
        )
    return "\n".join(lines)


def _markdown_aggregate_table(aggregate: list[dict]) -> str:
    lines = ["| " + " | ".join(_AGGREGATE_HEADERS) + " |", "|" + "---|" * len(_AGGREGATE_HEADERS)]
    for a in aggregate:
        lines.append(
            "| "
            + " | ".join(
                str(x)
                for x in [a["group"], a["tong_so_tc"], a["pass"], a["fail"], a["blocked"], a["na"], a["ti_le_pass"]]
            )
            + " |"
        )
    return "\n".join(lines)


def build_markdown_report(
    username: str,
    rows: list[ReportRow],
    aggregate: list[dict],
    run013_table: Optional[list[dict]],
    checklist: list[tuple[str, bool, str]],
) -> str:
    generated_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    parts = [
        "# Báo cáo kết quả kiểm thử nâng cấp Ceph 14.2.22 (Nautilus) -> 16.2.15 (Pacific)",
        "",
        f"Xuất lúc: {generated_at} | Người xuất: {username}",
        "",
        "> Lưu ý: bảng tổng hợp bên dưới dùng số liệu THỰC TẾ (POST=41, Tổng=71), khác với số liệu "
        "gốc trong docs/ceph-upgrade-test-cases.md mục 4 (POST=33, Tổng=63) -- tài liệu gốc bị sai số "
        "đếm cho nhóm POST, đã xác nhận bởi số lượng test case implement thực tế trong worker/executor/"
        "test_runner/group_b.py.",
        "",
        "## 1. Kết quả theo test case",
        "",
        _markdown_row_table(rows),
    ]
    if run013_table:
        parts += [
            "",
            "### Chi tiết TC-RUN-013 -- convert OMAP theo từng OSD",
            "",
            "| OSD ID | Thời gian (s) | Exit code | Vượt 2x ước lượng? |",
            "|---|---|---|---|",
        ]
        for entry in run013_table:
            parts.append(
                f"| {entry['osd_id']} | {entry['seconds']} | {entry['exit_code']} | "
                f"{'Có' if entry['over_estimate'] else 'Không'} |"
            )
    parts += [
        "",
        "## 2. Bảng tổng hợp",
        "",
        _markdown_aggregate_table(aggregate),
        "",
        "## 3. Tiêu chí kết thúc (Exit Criteria)",
        "",
    ]
    for item, checked, note in checklist:
        box = "[x]" if checked else "[ ]"
        line = f"- {box} {item}"
        if note:
            line += f" _(ghi chú: {note})_"
        parts.append(line)
    parts.append("")
    return "\n".join(parts)


def build_excel_workbook(username: str, rows: list[ReportRow], aggregate: list[dict]) -> bytes:
    wb = Workbook()
    ws1 = wb.active
    ws1.title = "Ket qua test case"  # openpyxl sheet titles: avoid diacritics for wider spreadsheet-app compatibility
    ws1.append(_ROW_HEADERS)
    for r in rows:
        ws1.append([r.document_id, r.ngay_thuc_hien, r.nguoi_thuc_hien, r.ket_qua_thuc_te, r.pass_fail, r.defect_id, r.ghi_chu])

    ws2 = wb.create_sheet("Tong hop")
    ws2.append(_AGGREGATE_HEADERS)
    for a in aggregate:
        ws2.append([a["group"], a["tong_so_tc"], a["pass"], a["fail"], a["blocked"], a["na"], a["ti_le_pass"]])

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def build_copy_summary_text(rows: list[ReportRow], aggregate: list[dict]) -> str:
    total = next(a for a in aggregate if a["group"] == "Tổng")
    p1_rows = [r for r in rows if r.priority == "P1"]
    p1_pass = sum(1 for r in p1_rows if r.pass_fail == "Pass")
    not_run = sum(1 for r in rows if r.pass_fail == "")
    lines = [
        f"Kết quả kiểm thử nâng cấp Ceph 14.2.22 -> 16.2.15: {total['pass']}/{total['tong_so_tc']} PASS "
        f"({total['ti_le_pass']}).",
        f"P1: {p1_pass}/{len(p1_rows)} PASS." if p1_rows else "P1: không có test case P1.",
        f"Chưa chạy/đang chạy: {not_run} test case.",
        "",
        _markdown_aggregate_table(aggregate),
    ]
    return "\n".join(lines)

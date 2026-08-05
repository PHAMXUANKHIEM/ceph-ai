import asyncio
import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from dashboard.routes.auth import require_login
from shared import db
from shared.cluster_nodes import configured_nodes as _configured_nodes
from shared.models import TestRunnerConfig
from shared import test_runner_baselines as baselines
from worker.executor.ssh_executor import ExecutorError, test_all_nodes
from worker.executor.test_runner.framework import (
    TestCase,
    TestCaseDeclined,
    TestCaseError,
    TestResult,
    TestRunContext,
    TestStatus,
    poll_test_case,
    run_test_case,
)
from worker.executor.test_runner import report as report_builder
from worker.executor.test_runner.registry import TEST_CASES_BY_ID, build_test_run_context, filter_selected

logger = logging.getLogger(__name__)

router = APIRouter()

# Epic 10 (Ceph Upgrade Test Runner) placeholder. This router intentionally
# has no test-case business logic yet -- 63 upgrade test cases, SSH
# execution via worker/executor/ssh_executor.py's new execute_with_retry/
# execute_background, the live SSE/WS log stream, and Markdown report
# export are future Epic 10 stories. This stub only proves the wiring: the
# ceph-upgrade-test-runner-frontend React app (relocated from the
# throwaway prototype, see its vite.config.js /api proxy) talks to THIS
# same dashboard/app.py FastAPI backend rather than a second standalone
# process, consistent with Epic 10's "one shared backend" decision.
#
# Deliberately no `Depends(require_login)` on this one endpoint -- it is a
# plain liveness probe (mirrors a typical /health endpoint), not a data or
# action route; every real Epic 10 endpoint added on top of this router
# later must gate on require_login like every other API route in
# dashboard/routes/ (see nodes.py, upgrade.py).
@router.get("/api/test-runner/health")
async def test_runner_health():
    return {"status": "ok"}


# BASELINE_FILE_KEYS/BASELINE_FILES_DIR now live in shared/test_runner_baselines.py
# (Story 10.4) so worker/executor/test_runner/group_b.py can read the same
# uploaded files without worker/ importing from dashboard/. Referenced below
# as `baselines.BASELINE_FILES_DIR` (qualified, via `import ... as baselines`)
# rather than `from shared.test_runner_baselines import BASELINE_FILES_DIR` --
# the latter would bind a SECOND, independent copy of the name in this
# module's own namespace, so a test that monkeypatches
# shared.test_runner_baselines.BASELINE_FILES_DIR (the one true source
# group_b.py's baseline_file_path()/read_baseline_text() actually read)
# would silently NOT affect this route file's own reads, and vice versa.
# Qualified access keeps exactly one binding, patchable from either side.
# Stored on disk keyed by the fixed key name (not the client's original
# filename) specifically to avoid a client-supplied filename being used to
# build a filesystem path (path-injection risk) -- see upload_baseline_file()
# below.


def _get_or_create_config(session) -> TestRunnerConfig:
    """Singleton-row lookup -- same "1 cluster at a time" posture as
    config/settings.py. Creates the row on first write (POST /config or a
    baseline upload) rather than requiring a separate init step."""
    config = session.query(TestRunnerConfig).first()
    if config is None:
        config = TestRunnerConfig()
        session.add(config)
        session.flush()
    return config


def _baseline_files_dict(config: TestRunnerConfig | None) -> dict:
    if config is None or not config.baseline_files:
        return {}
    try:
        return json.loads(config.baseline_files)
    except (TypeError, ValueError):
        return {}


def _config_response(config: TestRunnerConfig | None) -> dict:
    """Shared shape for GET /config -- also used to build the response
    right after a POST /config or baseline upload so callers get a fresh
    reflect-back without a second round trip."""
    baseline_present = _baseline_files_dict(config)
    return {
        "nodes": _configured_nodes(),
        "rgw_endpoint_zone_a": config.rgw_endpoint_zone_a if config else None,
        "rgw_endpoint_zone_b": config.rgw_endpoint_zone_b if config else None,
        "rgw_endpoint_vip": config.rgw_endpoint_vip if config else None,
        "client_host": config.client_host if config else None,
        "test_groups": json.loads(config.test_groups) if config and config.test_groups else [],
        "priorities": json.loads(config.priorities) if config and config.priorities else [],
        "baseline_files": {
            key: (key in baseline_present) for key in baselines.BASELINE_FILE_KEYS
        },
    }


@router.get("/api/test-runner/config")
async def get_test_runner_config(user: str = Depends(require_login)):
    with db.SessionLocal() as session:
        config = session.query(TestRunnerConfig).first()
        return JSONResponse(_config_response(config))


@router.post("/api/test-runner/config")
async def save_test_runner_config(request: Request, user: str = Depends(require_login)):
    """Upserts RGW endpoints + test-group/priority selection onto the
    singleton row. All fields optional -- no minimum enforced this story
    (see spec's I/O matrix). An explicit empty list for test_groups/
    priorities is preserved as-is (not silently defaulted back to "all
    selected") -- that's why these are only touched when the key is
    present in the payload at all, using None as "key absent". Same
    `await request.json()` JSON-body pattern other API routes in this
    codebase already use (see nodes.py, backups.py, deploy_cluster.py)."""
    payload = await request.json()
    with db.SessionLocal() as session:
        config = _get_or_create_config(session)
        if "rgw_endpoint_zone_a" in payload:
            config.rgw_endpoint_zone_a = payload.get("rgw_endpoint_zone_a") or None
        if "rgw_endpoint_zone_b" in payload:
            config.rgw_endpoint_zone_b = payload.get("rgw_endpoint_zone_b") or None
        if "rgw_endpoint_vip" in payload:
            config.rgw_endpoint_vip = payload.get("rgw_endpoint_vip") or None
        if "client_host" in payload:
            config.client_host = payload.get("client_host") or None
        if "test_groups" in payload:
            config.test_groups = json.dumps(payload.get("test_groups") or [])
        if "priorities" in payload:
            config.priorities = json.dumps(payload.get("priorities") or [])
        session.commit()
        session.refresh(config)
        return JSONResponse(_config_response(config))


@router.post("/api/test-runner/config/baseline/{baseline_key}")
async def upload_baseline_file(
    baseline_key: str, file: UploadFile = File(...), user: str = Depends(require_login)
):
    if baseline_key not in baselines.BASELINE_FILE_KEYS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"baseline_key không hợp lệ: {baseline_key!r} -- chỉ chấp nhận "
                f"{', '.join(baselines.BASELINE_FILE_KEYS)}."
            ),
        )

    baselines.BASELINE_FILES_DIR.mkdir(parents=True, exist_ok=True)
    # Saved under the fixed baseline_key, never the client-supplied
    # filename (file.filename) -- avoids path-injection risk from a
    # client-controlled name reaching the filesystem.
    dest_path = baselines.BASELINE_FILES_DIR / baseline_key
    raw_bytes = await file.read()
    dest_path.write_bytes(raw_bytes)

    with db.SessionLocal() as session:
        config = _get_or_create_config(session)
        baseline_present = _baseline_files_dict(config)
        # Stored as "<baseline_files_dir_name>/<key>" rather than an
        # absolute path or a path computed against PROJECT_ROOT -- keeps
        # this independent of exactly where BASELINE_FILES_DIR resolves to
        # (tests point it at a tmp dir).
        baseline_present[baseline_key] = f"{baselines.BASELINE_FILES_DIR.name}/{baseline_key}"
        config.baseline_files = json.dumps(baseline_present)
        session.commit()
        session.refresh(config)
        return JSONResponse(_config_response(config))


@router.post("/api/test-runner/ssh-check")
async def ssh_check(user: str = Depends(require_login)):
    """Live SSH connectivity check against every node Settings has
    configured -- wired to Story 10.1's test_all_nodes(), which already
    guarantees dict[host, bool] and never raises for a per-host failure.
    Zero configured nodes -> test_all_nodes([]) -> {} (no special-casing
    needed here; the frontend renders the "chưa cấu hình node" hint off an
    empty result)."""
    hosts = [node["host"] for node in _configured_nodes()]
    results = test_all_nodes(hosts)
    return JSONResponse(results)


# -----------------------------------------------------------------------
# Story 10.6: the run-state engine. Test cases execute directly inside THIS
# Dashboard process (never through Worker/RabbitMQ -- see
# worker/executor/test_runner/framework.py's own module docstring), and a
# background TestCase's opaque `state` typically wraps a live paramiko
# Channel (BackgroundCommandHandle) that cannot be serialized -- so all run
# state lives in this module-level dict, in memory only. It is intentionally
# NOT persisted to the DB and does NOT survive a Dashboard restart; Story
# 10.8 (SQLite persistence/auto-save) is the story that would need to design
# around that, not this one.
#
# `_run_lock` guards every read-modify-write of `_run_states` -- FastAPI can
# genuinely run route handlers concurrently (asyncio tasks, plus
# asyncio.to_thread handing blocking work to a real thread-pool thread), so
# the "is this test already RUNNING" check-and-set in run_test() and the
# "don't let a stray poll overwrite an override" guard in _apply_result()
# both need to happen under the same lock as any other reader/writer.
# -----------------------------------------------------------------------


@dataclass
class _RunState:
    status: str = TestStatus.RUNNING.value
    criteria: list = field(default_factory=list)
    raw_output: str = ""
    notes: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    background_state: Any = None
    overridden: bool = False
    override_note: str = ""


_run_states: dict[str, _RunState] = {}
_run_lock = threading.Lock()

_TERMINAL_STATUSES = {
    TestStatus.PASS.value,
    TestStatus.FAIL.value,
    TestStatus.ERROR.value,
    TestStatus.SKIP.value,
}


def _test_summary(test_case: TestCase) -> dict:
    with _run_lock:
        rs = _run_states.get(test_case.id)
    return {
        "id": test_case.id,
        "name": test_case.name,
        "group": test_case.group.value,
        "priority": test_case.priority.value,
        "background": test_case.background,
        "status": rs.status if rs else "not_started",
        "overridden": rs.overridden if rs else False,
    }


def _test_detail(test_case: TestCase) -> dict:
    summary = _test_summary(test_case)
    with _run_lock:
        rs = _run_states.get(test_case.id)
    if rs is None:
        summary.update(
            {"criteria": [], "raw_output": "", "notes": "", "override_note": "", "started_at": None, "finished_at": None}
        )
        return summary
    summary.update(
        {
            "criteria": rs.criteria,
            "raw_output": rs.raw_output,
            "notes": rs.notes,
            "override_note": rs.override_note,
            "started_at": rs.started_at,
            "finished_at": rs.finished_at,
        }
    )
    return summary


def _apply_result(test_id: str, result: TestResult, *, background_state: Any = None) -> None:
    """Writes a freshly computed TestResult into the run-state store.
    Sticky against overrides (AC6): if the operator has already manually
    closed this test out, a poll/run result that races in after must not
    silently reopen it. `raw_output` is APPENDED, never overwritten -- each
    poll of a background handle only ever returns the NEW output since the
    last read (BackgroundCommandHandle.read_new_output()'s contract), so the
    server is the only place a full transcript can be accumulated (AC5).
    """
    with _run_lock:
        rs = _run_states.setdefault(test_id, _RunState())
        if rs.overridden:
            return
        rs.status = result.status.value
        rs.criteria = [
            {"description": c.description, "passed": c.passed, "detail": c.detail} for c in result.criteria
        ]
        if result.raw_output:
            rs.raw_output += result.raw_output
        rs.notes = result.notes
        if result.started_at and not rs.started_at:
            rs.started_at = result.started_at.isoformat()
        if result.finished_at:
            rs.finished_at = result.finished_at.isoformat()
        rs.background_state = background_state


def _run_one_shot_sync(test_id: str, test_case: TestCase, ctx: TestRunContext) -> None:
    """Runs on a daemon thread (not asyncio.to_thread -- the HTTP request
    that triggered this must return immediately with a RUNNING status, not
    block until the test finishes; see Dev Notes on why this mirrors
    dashboard/routes/settings.py's `threading.Thread(target=proc.wait,
    daemon=True)` precedent rather than the codebase's more common
    `await asyncio.to_thread(...)` call sites, which all block the request).
    """
    try:
        result = run_test_case(test_case, ctx)
    except Exception as exc:  # noqa: BLE001 - defensive: run_test_case() already
        # catches TestCaseError/ExecutorError/TestCaseDeclined internally; anything
        # else escaping here is a genuine bug in the TestCase itself, and an
        # uncaught exception inside a daemon thread is otherwise silently
        # swallowed by the interpreter -- never surfacing as TestStatus.ERROR the
        # way run_test_case()'s own docstring says a real bug should.
        logger.exception("test case %s raised unexpectedly", test_id)
        result = TestResult(
            test_id=test_id,
            status=TestStatus.ERROR,
            notes=f"Lỗi không mong đợi: {exc}",
            finished_at=datetime.utcnow(),
        )
    _apply_result(test_id, result)


def _start_background_sync(test_case: TestCase, ctx: TestRunContext) -> tuple[Any, Optional[TestResult]]:
    """Runs `test_case.start(ctx)` -- unlike `.run()`/`.poll()`, framework.py
    has no wrapper for `.start()` (only run_test_case()/poll_test_case()
    exist), so TestCaseDeclined/TestCaseError/ExecutorError raised directly
    out of `.start()` must be caught here, mirroring the same three-way
    mapping run_test_case() applies to `.run()`. Returns (state, None) on
    success, or (None, error_result) if start itself failed -- the caller
    stores whichever applies.
    """
    try:
        state = test_case.start(ctx)
        return state, None
    except TestCaseDeclined as exc:
        return None, TestResult(
            test_id=test_case.id, status=TestStatus.SKIP, notes=str(exc), finished_at=datetime.utcnow()
        )
    except (TestCaseError, ExecutorError) as exc:
        return None, TestResult(
            test_id=test_case.id, status=TestStatus.ERROR, notes=str(exc), finished_at=datetime.utcnow()
        )


def _load_context() -> TestRunContext:
    with db.SessionLocal() as session:
        config = session.query(TestRunnerConfig).first()
        return build_test_run_context(config)


@router.get("/api/test-runner/tests")
async def list_tests(user: str = Depends(require_login)):
    """Filtered by the singleton TestRunnerConfig's saved test_groups/
    priorities -- an empty saved selection means "show all" (see
    registry.filter_selected()'s docstring), not "show none": a fresh/
    never-saved config row has both fields empty by construction, and
    nobody saves an explicit empty selection intending to run zero tests.
    """
    with db.SessionLocal() as session:
        config = session.query(TestRunnerConfig).first()
        test_groups = json.loads(config.test_groups) if config and config.test_groups else []
        priorities = json.loads(config.priorities) if config and config.priorities else []
    # Pass this module's own TEST_CASES_BY_ID explicitly (qualified) rather
    # than letting filter_selected() read registry.py's copy -- see
    # filter_selected()'s own docstring for why (a test-time monkeypatch of
    # TEST_CASES_BY_ID here would otherwise silently not apply).
    selected = sorted(filter_selected(TEST_CASES_BY_ID, test_groups, priorities), key=lambda tc: tc.id)
    return JSONResponse({"tests": [_test_summary(tc) for tc in selected]})


@router.post("/api/test-runner/tests/{test_id}/run")
async def run_test(test_id: str, user: str = Depends(require_login)):
    """Triggers a test case regardless of whether it's inside the CURRENT
    saved group/priority selection (AC2) -- the list endpoint's filter is
    presentation-only; narrowing the filter after a run has started must not
    strand an in-flight test with no way to check on it.

    One-shot tests execute on a fire-and-forget daemon thread
    (_run_one_shot_sync) so this request returns immediately with a RUNNING
    status. Background tests call `.start()` via asyncio.to_thread (real,
    if quick, SSH work) and store the returned opaque state server-side;
    actual progress is observed by polling GET .../result (AC3), not by this
    endpoint.
    """
    test_case = TEST_CASES_BY_ID.get(test_id)
    if test_case is None:
        raise HTTPException(status_code=404, detail=f"test id không hợp lệ: {test_id!r}")

    with _run_lock:
        existing = _run_states.get(test_id)
        if existing is not None and existing.status == TestStatus.RUNNING.value:
            raise HTTPException(status_code=409, detail=f"{test_id} đang chạy, đợi hoàn tất trước khi chạy lại")
        _run_states[test_id] = _RunState(status=TestStatus.RUNNING.value, started_at=datetime.utcnow().isoformat())

    ctx = await asyncio.to_thread(_load_context)

    if test_case.background:
        state, error_result = await asyncio.to_thread(_start_background_sync, test_case, ctx)
        if error_result is not None:
            _apply_result(test_id, error_result)
        else:
            with _run_lock:
                rs = _run_states.get(test_id)
                if rs is not None and not rs.overridden:
                    rs.background_state = state
    else:
        threading.Thread(target=_run_one_shot_sync, args=(test_id, test_case, ctx), daemon=True).start()

    return JSONResponse(_test_detail(test_case))


@router.get("/api/test-runner/tests/{test_id}/result")
async def get_test_result(test_id: str, user: str = Depends(require_login)):
    """For a background test still RUNNING (and not overridden), performs
    exactly one poll_test_case() tick per call -- matching framework.py's
    documented contract that the CALLER is responsible for calling poll()
    repeatedly (the caller is this endpoint, driven by the frontend's
    periodic fetch; there is no separate server-side poll loop/scheduler).
    Once a background test reaches a terminal status (pass/fail/error/skip)
    or has been manually overridden, further GETs stop polling and just
    return the stored result -- polling a finished/closed test again would
    serve no purpose and, for a still-live handle, would keep draining
    output nobody will read.
    """
    test_case = TEST_CASES_BY_ID.get(test_id)
    if test_case is None:
        raise HTTPException(status_code=404, detail=f"test id không hợp lệ: {test_id!r}")

    with _run_lock:
        rs = _run_states.get(test_id)
        should_poll = (
            rs is not None
            and test_case.background
            and not rs.overridden
            and rs.status not in _TERMINAL_STATUSES
            and rs.background_state is not None
        )
        background_state = rs.background_state if should_poll else None

    if should_poll:
        ctx = await asyncio.to_thread(_load_context)
        new_state, result = await asyncio.to_thread(poll_test_case, test_case, ctx, background_state)
        _apply_result(test_id, result, background_state=new_state)

    return JSONResponse(_test_detail(test_case))


@router.post("/api/test-runner/tests/{test_id}/override")
async def override_test_result(test_id: str, request: Request, user: str = Depends(require_login)):
    """FR37: manual Pass/Fail override -- needed because TestResult.decide_status()
    deliberately never auto-promotes a criterion with passed=None (no
    baseline collected, or an inherent human judgment call), so many test
    cases would otherwise sit at RUNNING forever. Sticky: see _apply_result()
    and get_test_result()'s should_poll guard -- once overridden, a later
    automatic poll will neither overwrite it nor resume polling."""
    test_case = TEST_CASES_BY_ID.get(test_id)
    if test_case is None:
        raise HTTPException(status_code=404, detail=f"test id không hợp lệ: {test_id!r}")

    payload = await request.json()
    status = payload.get("status")
    if status not in ("pass", "fail"):
        raise HTTPException(status_code=400, detail="status phải là 'pass' hoặc 'fail'")
    note = (payload.get("note") or "").strip()

    with _run_lock:
        rs = _run_states.setdefault(test_id, _RunState())
        rs.status = TestStatus.PASS.value if status == "pass" else TestStatus.FAIL.value
        rs.overridden = True
        rs.override_note = note
        rs.finished_at = datetime.utcnow().isoformat()

    return JSONResponse(_test_detail(test_case))


# -----------------------------------------------------------------------
# Story 10.7: report export. Read-only over the run-state store above --
# never writes to _run_states. worker/executor/test_runner/report.py's
# functions take plain dicts, so _run_states (dataclasses) is converted to
# plain dicts here rather than leaking the _RunState type into worker/ (see
# report.py's own module docstring on why it must stay dashboard/-free).
# -----------------------------------------------------------------------


def _run_states_snapshot() -> dict[str, dict]:
    with _run_lock:
        return {
            test_id: {
                "status": rs.status,
                "criteria": rs.criteria,
                "raw_output": rs.raw_output,
                "notes": rs.notes,
                "started_at": rs.started_at,
                "finished_at": rs.finished_at,
                "background_state": rs.background_state,
                "overridden": rs.overridden,
                "override_note": rs.override_note,
            }
            for test_id, rs in _run_states.items()
        }


def _build_report_context(username: str):
    run_states = _run_states_snapshot()
    rows = report_builder.build_report_rows(run_states, TEST_CASES_BY_ID, username)
    aggregate = report_builder.build_aggregate_table(rows)
    return run_states, rows, aggregate


@router.get("/api/test-runner/report/markdown")
async def download_report_markdown(user: str = Depends(require_login)):
    run_states, rows, aggregate = _build_report_context(user)
    run013_table = report_builder.build_run013_osd_table(run_states)
    checklist = report_builder.build_exit_criteria_checklist(rows)
    markdown = report_builder.build_markdown_report(user, rows, aggregate, run013_table, checklist)
    return PlainTextResponse(
        markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="bao-cao-nang-cap-ceph.md"'},
    )


@router.get("/api/test-runner/report/excel")
async def download_report_excel(user: str = Depends(require_login)):
    _run_states, rows, aggregate = _build_report_context(user)
    xlsx_bytes = report_builder.build_excel_workbook(user, rows, aggregate)
    return Response(
        xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="bao-cao-nang-cap-ceph.xlsx"'},
    )


@router.get("/api/test-runner/report/summary")
async def get_report_summary(user: str = Depends(require_login)):
    _run_states, rows, aggregate = _build_report_context(user)
    return JSONResponse({"summary_text": report_builder.build_copy_summary_text(rows, aggregate)})

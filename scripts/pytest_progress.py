"""pytest plugin: publish live test-run progress for the Dashboard.

Enabled only when CEPH_AI_TEST_PROGRESS_FILE is set, e.g.

    CEPH_AI_TEST_PROGRESS_FILE=/var/lib/ceph-ai/test-runs/current.json \\
    CEPH_AI_TEST_PROGRESS_LABEL="pre-push $(git rev-parse --short HEAD)" \\
    python -m pytest -p scripts.pytest_progress tests/

The JSON file is replaced atomically at most once per second and once more
when the session ends; /test-progress reads it (shared/test_progress.py).

The pre-push pipeline also records the steps around the test run, so the
page does not show the previous run while the quality gate is working:

    python scripts/pytest_progress.py begin "pre-push abc1234 subject"
    python scripts/pytest_progress.py step "Quality gate"
    python scripts/pytest_progress.py fail "Budget vượt trần"   # or: step / finish

The test run then continues the same record when its label matches.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ENV_FILE = "CEPH_AI_TEST_PROGRESS_FILE"
ENV_LABEL = "CEPH_AI_TEST_PROGRESS_LABEL"
_MAX_FAILURES = 50
_WRITE_INTERVAL_SECONDS = 1.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_state(label: str, status: str) -> dict:
    return {
        "schema": "ceph-ai.test-progress.v1",
        "label": label,
        "pid": os.getpid(),
        "status": status,
        "started_at": _now(),
        "updated_at": _now(),
        "finished_at": None,
        "total": 0,
        "done": 0,
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "errors": 0,
        "current": None,
        "failures": [],
        "steps": [],
    }


def _read(path: Path) -> dict | None:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def _write_atomic(path: Path, state: dict) -> None:
    state["updated_at"] = _now()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    temporary.chmod(0o644)
    os.replace(temporary, path)


def _close_running_step(state: dict, outcome: str) -> None:
    for step in state.get("steps") or []:
        if step.get("state") == "running":
            step["state"] = outcome
            step["finished_at"] = _now()


def _open_step(state: dict, name: str) -> None:
    _close_running_step(state, "ok")
    state.setdefault("steps", []).append({"name": name, "state": "running", "started_at": _now()})
    state["stage"] = name


class ProgressReporter:
    def __init__(self, path: Path, label: str) -> None:
        self.path = path
        self.state: dict = _empty_state(label, "collecting")
        previous = _read(path)
        # Continue the pipeline's record (quality gate, budget...) of this run.
        if previous and previous.get("label") == label and previous.get("status") == "preparing":
            self.state["started_at"] = previous.get("started_at") or self.state["started_at"]
            self.state["steps"] = list(previous.get("steps") or [])
        _open_step(self.state, "Test")
        self._last_write = 0.0
        self.write(force=True)

    def write(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_write < _WRITE_INTERVAL_SECONDS:
            return
        self._last_write = now
        _write_atomic(self.path, self.state)

    def pytest_collection_finish(self, session) -> None:
        self.state["total"] = len(session.items)
        self.state["status"] = "running"
        self.write(force=True)

    def pytest_runtest_logstart(self, nodeid, location) -> None:
        self.state["current"] = nodeid
        self.write()

    def pytest_runtest_logreport(self, report) -> None:
        failed = report.failed
        if report.when == "call" or (report.when == "setup" and (failed or report.skipped)):
            self.state["done"] += 1
            if report.passed:
                self.state["passed"] += 1
            elif report.skipped:
                self.state["skipped"] += 1
            elif report.when == "setup":
                self.state["errors"] += 1
            else:
                self.state["failed"] += 1
        elif report.when == "teardown" and failed:
            self.state["errors"] += 1
        if failed and len(self.state["failures"]) < _MAX_FAILURES:
            self.state["failures"].append({"nodeid": report.nodeid, "when": report.when})
        self.write()

    def pytest_sessionfinish(self, session, exitstatus) -> None:
        self.state["current"] = None
        self.state["finished_at"] = _now()
        self.state["exit_status"] = int(exitstatus)
        self.state["status"] = "passed" if int(exitstatus) == 0 else "failed"
        _close_running_step(self.state, "ok" if int(exitstatus) == 0 else "failed")
        self.write(force=True)


def pytest_configure(config) -> None:
    path = os.environ.get(ENV_FILE)
    if path:
        config.pluginmanager.register(
            ProgressReporter(Path(path), os.environ.get(ENV_LABEL, "")), "ceph_ai_progress_reporter",
        )


def record_pipeline(path: Path, command: str, text: str) -> None:
    """``begin`` a run, open a ``step``, ``fail`` it with a reason or ``finish`` it."""
    if command == "begin":
        state = _empty_state(text, "preparing")
    else:
        state = _read(path) or _empty_state("", "preparing")
    if command == "step":
        _open_step(state, text)
    elif command == "fail":
        _close_running_step(state, "failed")
        state["status"] = "aborted"
        state["stage"] = text
        state["finished_at"] = _now()
    elif command == "finish":
        _close_running_step(state, "ok")
        state["stage"] = None
    elif command != "begin":
        raise ValueError(f"unknown command {command!r}")
    _write_atomic(path, state)


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in {"begin", "step", "fail", "finish"}:
        print("usage: pytest_progress.py begin LABEL | step NAME | fail REASON | finish -", file=sys.stderr)
        return 2
    path = os.environ.get(ENV_FILE)
    if not path:
        return 0
    record_pipeline(Path(path), argv[0], argv[1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

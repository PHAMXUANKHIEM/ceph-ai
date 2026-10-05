"""pytest plugin: publish live test-run progress for the Dashboard.

Enabled only when CEPH_AI_TEST_PROGRESS_FILE is set, e.g.

    CEPH_AI_TEST_PROGRESS_FILE=/var/lib/ceph-ai/test-runs/current.json \\
    CEPH_AI_TEST_PROGRESS_LABEL="pre-push $(git rev-parse --short HEAD)" \\
    python -m pytest -p scripts.pytest_progress tests/

The JSON file is replaced atomically at most once per second and once more
when the session ends; /test-progress reads it (shared/test_progress.py).
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

ENV_FILE = "CEPH_AI_TEST_PROGRESS_FILE"
ENV_LABEL = "CEPH_AI_TEST_PROGRESS_LABEL"
_MAX_FAILURES = 50
_WRITE_INTERVAL_SECONDS = 1.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProgressReporter:
    def __init__(self, path: Path, label: str) -> None:
        self.path = path
        self.state: dict = {
            "schema": "ceph-ai.test-progress.v1",
            "label": label,
            "pid": os.getpid(),
            "status": "collecting",
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
        }
        self._last_write = 0.0
        self.write(force=True)

    def write(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_write < _WRITE_INTERVAL_SECONDS:
            return
        self._last_write = now
        self.state["updated_at"] = _now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
        temporary.chmod(0o644)
        os.replace(temporary, self.path)

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
        self.write(force=True)


def pytest_configure(config) -> None:
    path = os.environ.get(ENV_FILE)
    if path:
        config.pluginmanager.register(
            ProgressReporter(Path(path), os.environ.get(ENV_LABEL, "")), "ceph_ai_progress_reporter",
        )

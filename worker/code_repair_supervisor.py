"""Continuously trigger bounded Code Repair from newly appended app logs.

This process deliberately lives outside Watcher/Worker. Candidate deployments
restart those services, while this supervisor must survive long enough to
evaluate the deployment and promote or roll it back.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from config.settings import settings
from worker.code_repair import ERROR_RE, SECRET_RE, RepairConfig, run_repair
from worker import ceph_capability_learning as ceph_learning

logger = logging.getLogger(__name__)


@dataclass
class Cursor:
    inode: int
    offset: int


def _load_cursors(path: Path) -> dict[str, Cursor]:
    try:
        raw = json.loads(path.read_text())
        return {name: Cursor(int(value["inode"]), int(value["offset"])) for name, value in raw.items()}
    except (FileNotFoundError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return {}


def _save_cursors(path: Path, cursors: dict[str, Cursor]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({name: vars(cursor) for name, cursor in cursors.items()}, indent=2))
    os.replace(temporary, path)


def read_new_errors(paths: list[Path], cursors: dict[str, Cursor], *, initialize_at_end: bool) -> list[str]:
    """Read only appended bytes, skipping historical errors on first startup."""
    evidence: list[str] = []
    for path in paths:
        if "code-repair" in path.name or not path.is_file():
            continue
        stat = path.stat()
        key = str(path)
        cursor = cursors.get(key)
        if cursor is None:
            offset = stat.st_size if initialize_at_end else 0
        elif cursor.inode != stat.st_ino or stat.st_size < cursor.offset:
            offset = 0  # rotation/truncation: the replacement file is new data
        else:
            offset = cursor.offset
        with path.open("r", errors="replace") as handle:
            handle.seek(offset)
            appended = handle.read(250_000)
            cursors[key] = Cursor(stat.st_ino, handle.tell())
        matches = list(ERROR_RE.finditer(appended))
        if matches:
            block = appended[max(0, matches[-1].start() - 1200):matches[-1].start() + 16_000]
            block = SECRET_RE.sub(lambda match: match.group(1) + "=<redacted>", block)
            evidence.append(f"Source application log: {path.name}\n{block}")
    return evidence


def run_forever(*, max_iterations: int | None = None) -> None:
    repo = Path(__file__).resolve().parents[1]
    cursor_file = Path(settings.code_repair_cursor_file)
    cursors = _load_cursors(cursor_file)
    first_scan = not cursor_file.exists()
    learning_state_file = Path(settings.ceph_capability_learning_state_file)
    learning_state = ceph_learning.load_state(learning_state_file)
    if settings.ceph_capability_learning_enabled and not learning_state.get("initialized"):
        if not settings.ceph_capability_learning_include_existing:
            for dedupe_key in ceph_learning.eligible_keys():
                learning_state.setdefault("findings", {})[dedupe_key] = {"status": "BASELINED"}
        learning_state["initialized"] = True
        ceph_learning.save_state(learning_state_file, learning_state)
    iterations = 0
    while settings.code_repair_auto_enabled:
        paths = sorted(Path("/var/log").glob("ceph-ai-*.log"))
        errors = read_new_errors(paths, cursors, initialize_at_end=first_scan)
        first_scan = False
        _save_cursors(cursor_file, cursors)
        if errors:
            result = run_repair(
                max(errors, key=len),
                RepairConfig(
                    repo=repo,
                    provider=settings.code_repair_provider,
                    test_command=settings.code_repair_test_command,
                    timeout_seconds=settings.code_repair_timeout_seconds,
                    push=settings.code_repair_push,
                    deploy_staging=settings.code_repair_deploy_staging,
                    promote_main=settings.code_repair_promote_main,
                ),
            )
            logger.info("automatic Code Repair completed: %s (%s)", result.status, result.fingerprint)
        elif settings.ceph_capability_learning_enabled:
            seen = set(learning_state.setdefault("findings", {}))
            candidate = ceph_learning.next_candidate(seen)
            if candidate is not None:
                ceph_learning.mark(learning_state, candidate, "RUNNING")
                ceph_learning.save_state(learning_state_file, learning_state)
                result = run_repair(
                    candidate.evidence,
                    RepairConfig(
                        repo=repo,
                        provider=settings.code_repair_provider,
                        test_command=settings.code_repair_test_command,
                        timeout_seconds=settings.code_repair_timeout_seconds,
                        push=settings.code_repair_push,
                        deploy_staging=settings.code_repair_deploy_staging,
                        promote_main=settings.code_repair_promote_main,
                        task_kind="ceph-capability-learning",
                        task_instructions=ceph_learning.LEARNING_INSTRUCTIONS,
                    ),
                )
                ceph_learning.mark(learning_state, candidate, result.status)
                ceph_learning.save_state(learning_state_file, learning_state)
                logger.info(
                    "Ceph capability learning completed: %s finding=%s fingerprint=%s",
                    result.status, candidate.finding_id, result.fingerprint,
                )
        iterations += 1
        if max_iterations is not None and iterations >= max_iterations:
            return
        time.sleep(max(5, settings.code_repair_poll_interval_seconds))


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
    if not settings.code_repair_auto_enabled:
        logger.info("automatic Code Repair is disabled")
        return 0
    lock_path = Path(settings.code_repair_lock_file)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info("another Code Repair supervisor already holds the lock")
            return 0
        run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

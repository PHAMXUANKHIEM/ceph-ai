"""systemd-owned runner for an explicit AI Development Task."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from config.settings import settings
from worker.code_repair import RepairConfig, run_repair


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TASK_ROOT = Path("/var/lib/ceph-ai/ai-tasks")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def main(task_id: str) -> int:
    directory = TASK_ROOT / task_id
    metadata_path = directory / "task.json"
    state_path = directory / "state.json"
    try:
        metadata = json.loads(metadata_path.read_text())
        prompt = (directory / "prompt.txt").read_text(errors="replace")
        instructions = (directory / "instructions.txt").read_text(errors="replace")
        config = RepairConfig(
            repo=PROJECT_ROOT,
            provider="auto",
            planner_provider=metadata["planner_provider"],
            planner_model=metadata["planner_model"],
            planner_account_profile=metadata["planner_account_profile"],
            implementer_provider=metadata["implementer_provider"],
            implementer_model=metadata["implementer_model"],
            implementer_account_profile=metadata["implementer_account_profile"],
            max_review_rounds=int(metadata["max_review_rounds"]),
            test_command=settings.code_repair_test_command,
            timeout_seconds=settings.code_repair_timeout_seconds,
            push=bool(metadata.get("push_branch")),
            state_file=state_path,
            task_kind="user-request",
            task_instructions=instructions,
            notify_telegram=False,
        )
        result = run_repair(prompt, config, force=True)
        metadata.update({
            "status": result.status,
            "finished_at": _now(),
            "result": {
                "branch": result.branch,
                "commit": result.commit,
                "error": result.error,
            },
        })
        _write_json(metadata_path, metadata)
        return 0 if result.status in {"COMMITTED", "PUSHED"} else 1
    except Exception as exc:
        metadata = locals().get("metadata", {"task_id": task_id})
        metadata.update({"status": "FAILED", "error": str(exc), "finished_at": _now()})
        _write_json(metadata_path, metadata)
        return 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m worker.ai_task_runner TASK_ID")
    raise SystemExit(main(sys.argv[1]))

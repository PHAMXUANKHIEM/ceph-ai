#!/usr/bin/python3.11
"""FL6.2: ask the AI how to reproduce an under-evidenced fault family on the lab.

Runs on the host, like the nightly analysts (the AI CLI and its accounts live
there). For one family from the evidence-gap queue it fetches the official
Ceph text, asks the AI (read-only CLI, no repository access) for a JSON
proposal, validates it and stores it under PROPOSALS_DIR. Nothing is
injected: an operator approves a stored proposal on Telegram (FL6.3).

    python -m scripts.lab.propose_reproduction --family pg_peering
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

PROPOSALS_DIR = Path("/var/lib/ceph-ai/failure-lab/proposals")
AI_TIMEOUT_SECONDS = 600


def ask_ai(prompt: str, *, timeout_seconds: int = AI_TIMEOUT_SECONDS) -> str:
    """The final answer of the planner AI, run read-only in an empty directory."""
    from config.settings import settings
    from shared.ai_budget import check as check_ai_budget
    from shared.ai_observability import record_ai_attempt
    from worker.code_repair import RepairConfig, _ai_process_environment, _provider_command, _role_account_dirs, _run
    from worker.code_repair_supervisor import _configured_account_profile, _final_answer, _with_last_message_file

    provider = settings.code_repair_planner_provider or settings.code_repair_provider
    model = settings.code_repair_planner_model or ""
    profile = _configured_account_profile(settings.code_repair_planner_account_source,
                                          settings.code_repair_planner_account_profile)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="ceph-ai-fl6-") as root:
        workdir = Path(root)
        codex_home, claude_config_dir = _role_account_dirs(RepairConfig(repo=workdir, planner_account_profile=profile),
                                                           profile)
        selected, command = _provider_command(provider, workdir, prompt, timeout_seconds, claude_config_dir=claude_config_dir,
                                              codex_home=codex_home, model=model, mode="review")
        last_message = workdir / "last-message.txt"
        command = _with_last_message_file(selected, command, last_message)
        reservation = check_ai_budget(selected, model or "default", len(prompt))
        with _ai_process_environment() as env:
            result = _run(command, cwd=workdir, timeout=timeout_seconds, input_text=prompt, check=False, env=env)
        answer = _final_answer(last_message, result.stdout or "")
        record_ai_attempt(reservation_id=reservation, feature="failure_lab_reproduction_proposal", provider=selected,
                          model_id=model or "default", status="SUCCESS" if result.returncode == 0 else "ERROR",
                          latency_ms=round((time.monotonic() - started) * 1000), input_chars=len(prompt),
                          output_chars=len(answer))
    if result.returncode != 0:
        raise RuntimeError(f"{selected} exited with {result.returncode}")
    return answer


def propose(family: str, *, ask=ask_ai, page: str | None = None, days: int = 30) -> dict:
    from shared import ceph_docs, db
    from shared.evidence_gaps import evidence_gap_queue
    from shared.failure_lab_fault import fault_scenarios
    from shared.reproduction_proposal import build_prompt, parse, validate
    from watcher.incident_correlation import FAMILY_CODES
    from worker.policy import gate

    if family not in FAMILY_CODES:
        raise SystemExit(f"unknown family {family!r}; known: {', '.join(sorted(FAMILY_CODES))}")
    fault_codes = {key: set(value.expected_health_codes) for key, value in fault_scenarios().items()}
    with db.SessionLocal() as session:
        gap = next((item for item in evidence_gap_queue(session, family_codes=FAMILY_CODES, fault_codes=fault_codes,
                                                       days=days) if item.family == family), None)
    citations = ceph_docs.citations_for(page if page is not None else ceph_docs.health_checks_page(),
                                        FAMILY_CODES[family])
    # Destructive actions are never an acceptable answer for a lab reproduction;
    # "investigate only" always is.
    action_ids = sorted(gate.READ_ONLY_ACTION_IDS | gate.SAFE_ACTION_IDS | gate.RISKY_ACTION_IDS
                        | {"investigate_manually"})
    raw = parse(ask(build_prompt(family, gap.examples if gap else [], citations, action_ids)))
    proposal, errors = validate(raw, family=family, given_urls={item["url"] for item in citations},
                                action_ids=set(action_ids), family_prefixes=FAMILY_CODES[family])
    record = {
        "id": f"repro-{uuid.uuid4().hex[:10]}", "family": family,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "PROPOSED" if proposal is not None and not errors else
                  ("NEW_KIND_REQUESTED" if proposal is not None else "REJECTED"),
        "proposal": proposal.as_dict() if proposal is not None else raw, "validation_errors": errors,
        "evidence_examples": gap.examples if gap else [], "documents": [item["url"] for item in citations],
    }
    return record


def save(record: dict, directory: Path = PROPOSALS_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{record['id']}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--family", required=True)
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()
    record = propose(args.family, days=args.days)
    path = save(record)
    print(json.dumps({"saved": str(path), "status": record["status"], "errors": record["validation_errors"]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

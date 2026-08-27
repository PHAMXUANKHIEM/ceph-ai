"""Export verified production labels or score offline AI predictions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from shared.ai_evaluation import evaluate


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(f"{path}:{number}: expected JSON object")
                rows.append(value)
    return rows


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def _verified_rows(limit: int) -> tuple[list[dict], list[dict]]:
    from shared.db import SessionLocal
    from shared.models import Action, RemediationCase

    with SessionLocal() as session:
        items = (
            session.query(RemediationCase, Action)
            .join(Action, Action.id == RemediationCase.action_id)
            .filter(RemediationCase.verified_at.isnot(None))
            .order_by(RemediationCase.verified_at.desc(), RemediationCase.id)
            .limit(max(1, limit)).all()
        )
        rows, predictions = [], []
        for case, action in items:
            successful = case.outcome == "VERIFIED_SUCCESS" and case.operator_verdict not in {"FALSE_POSITIVE", "UNSAFE"}
            rows.append({
                "id": case.id, "fault_family": case.fault_family,
                "expected_action_id": action.action_id if successful else None,
                "should_act": successful, "outcome": case.outcome,
                "operator_verdict": case.operator_verdict,
                "prompt_version": case.prompt_version,
                "model_provider": case.model_provider,
            })
            predictions.append({
                "id": case.id, "action_id": action.action_id,
                "confidence": case.diagnosis_confidence,
            })
    return rows, predictions


def export_verified(path: Path, limit: int) -> int:
    rows, _predictions = _verified_rows(limit)
    _write_jsonl(path, rows)
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export-verified")
    export.add_argument("output", type=Path); export.add_argument("--limit", type=int, default=1000)
    score = sub.add_parser("score")
    score.add_argument("golden", type=Path); score.add_argument("predictions", type=Path)
    production = sub.add_parser("production-report")
    production.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()
    if args.command == "export-verified":
        print(json.dumps({"exported": export_verified(args.output, args.limit), "output": str(args.output)}))
    elif args.command == "score":
        print(json.dumps(evaluate(_read_jsonl(args.golden), _read_jsonl(args.predictions)).as_dict(), sort_keys=True))
    else:
        golden, predictions = _verified_rows(args.limit)
        print(json.dumps(evaluate(golden, predictions).as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

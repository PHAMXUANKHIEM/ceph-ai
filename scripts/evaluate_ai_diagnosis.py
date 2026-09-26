"""Export verified production labels or score offline AI predictions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from shared.ai_evaluation import evaluate, evaluate_by

POLICY_PATH = Path(__file__).resolve().parents[1] / "worker" / "policy" / "action_policy.yaml"


def action_catalogue(path: Path = POLICY_PATH) -> frozenset[str]:
    """The playbook action catalogue; proposals outside it are hallucinations."""
    import yaml

    policy = yaml.safe_load(path.read_text(encoding="utf-8"))
    return frozenset(policy["action_ids"])


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
            .filter(RemediationCase.outcome.in_(("VERIFIED_SUCCESS", "VERIFIED_FAILED")))
            .order_by(RemediationCase.verified_at.desc(), RemediationCase.id)
            .limit(max(1, limit)).all()
        )
        rows, predictions = [], []
        for case, action in items:
            verdict = case.operator_verdict
            should_act = True if verdict == "CORRECT" else False if verdict in {"FALSE_POSITIVE", "UNSAFE"} else None
            diagnosis_correct = True if verdict == "CORRECT" else False if verdict == "FALSE_POSITIVE" else None
            rows.append({
                "id": case.id, "fault_family": case.fault_family, "health_code": case.fault_family,
                "expected_action_id": action.action_id if should_act is True else None,
                "should_act": should_act, "diagnosis_correct": diagnosis_correct,
                "outcome": case.outcome, "operator_verdict": verdict,
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


def _production_report(limit: int) -> dict:
    golden, predictions = _verified_rows(limit)
    catalogue = action_catalogue()
    providers = sorted({row.get("model_provider") or "unknown" for row in golden})
    by_provider = {}
    for provider in providers:
        ids = {row["id"] for row in golden if (row.get("model_provider") or "unknown") == provider}
        by_provider[provider] = evaluate(
            [row for row in golden if row["id"] in ids],
            [row for row in predictions if row["id"] in ids],
            known_action_ids=catalogue,
        ).as_dict()
    ai_ids = {
        row["id"] for row in golden
        if (row.get("model_provider") or "unknown") not in {"deterministic-controller", "unknown"}
    }
    ai_golden = [row for row in golden if row["id"] in ai_ids]
    ai_predictions = [row for row in predictions if row["id"] in ai_ids]
    ai_report = evaluate(ai_golden, ai_predictions, known_action_ids=catalogue).as_dict()
    return {
        "ai_only": ai_report,
        "by_provider": by_provider,
        "by_health_code": evaluate_by(ai_golden, ai_predictions, "health_code", known_action_ids=catalogue),
        "by_prompt_version": evaluate_by(ai_golden, ai_predictions, "prompt_version", known_action_ids=catalogue),
        "note": "null metrics mean operator labels are unavailable",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export-verified")
    export.add_argument("output", type=Path); export.add_argument("--limit", type=int, default=1000)
    score = sub.add_parser("score")
    score.add_argument("golden", type=Path); score.add_argument("predictions", type=Path)
    score.add_argument("--group-by", help="golden field to break the report down by, e.g. health_code")
    score.add_argument("--no-catalogue", action="store_true", help="do not score hallucinated action ids")
    production = sub.add_parser("production-report")
    production.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()
    if args.command == "export-verified":
        print(json.dumps({"exported": export_verified(args.output, args.limit), "output": str(args.output)}))
    elif args.command == "score":
        golden, predictions = _read_jsonl(args.golden), _read_jsonl(args.predictions)
        catalogue = None if args.no_catalogue else action_catalogue()
        report = {"overall": evaluate(golden, predictions, known_action_ids=catalogue).as_dict()}
        if args.group_by:
            report[f"by_{args.group_by}"] = evaluate_by(golden, predictions, args.group_by, known_action_ids=catalogue)
        print(json.dumps(report, sort_keys=True))
    else:
        print(json.dumps(_production_report(args.limit), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

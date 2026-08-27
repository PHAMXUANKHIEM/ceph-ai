"""Strict, deterministic offline metrics for AI diagnosis evaluations."""
from __future__ import annotations
from dataclasses import asdict, dataclass

@dataclass(frozen=True)
class EvaluationReport:
    total: int; matched: int; actionable_labels: int; abstention_labels: int
    diagnosis_labels: int; action_accuracy: float | None; abstention_recall: float | None
    unsafe_negative_rate: float | None; unsafe_overall_rate: float
    diagnosis_brier_score: float | None
    def as_dict(self) -> dict: return asdict(self)

def _index_unique(rows: list[dict], kind: str) -> dict[str, dict]:
    result = {}
    for number, row in enumerate(rows, 1):
        if not isinstance(row, dict): raise ValueError(f"{kind} row {number} must be an object")
        case_id = row.get("id")
        if not isinstance(case_id, str) or not case_id.strip(): raise ValueError(f"{kind} row {number} requires a non-empty string id")
        if case_id in result: raise ValueError(f"duplicate {kind} id {case_id!r}")
        result[case_id] = row
    return result

def _validate_truth(row: dict) -> None:
    should_act, diagnosis_correct = row.get("should_act"), row.get("diagnosis_correct")
    if should_act is not None and not isinstance(should_act, bool): raise ValueError(f"should_act must be boolean or null for case {row['id']!r}")
    if diagnosis_correct is not None and not isinstance(diagnosis_correct, bool): raise ValueError(f"diagnosis_correct must be boolean or null for case {row['id']!r}")
    expected = row.get("expected_action_id")
    if should_act is True and (not isinstance(expected, str) or not expected.strip()): raise ValueError(f"actionable case {row['id']!r} requires expected_action_id")
    if should_act is False and expected is not None: raise ValueError(f"abstention case {row['id']!r} cannot have expected_action_id")

def _validate_prediction(row: dict) -> tuple[str | None, bool, float | None]:
    action, abstain = row.get("action_id"), row.get("abstain", False)
    if action is not None and (not isinstance(action, str) or not action.strip()): raise ValueError(f"invalid action_id for case {row['id']!r}")
    if not isinstance(abstain, bool): raise ValueError(f"abstain must be boolean for case {row['id']!r}")
    if abstain and action: raise ValueError(f"case {row['id']!r} cannot both abstain and propose an action")
    confidence = row.get("confidence")
    if confidence is None: return action, abstain or not action, None
    if isinstance(confidence, bool): raise ValueError(f"invalid confidence for case {row['id']!r}")
    try: value = float(confidence)
    except (TypeError, ValueError) as exc: raise ValueError(f"invalid confidence for case {row['id']!r}") from exc
    if not 0 <= value <= 1: raise ValueError(f"confidence outside [0,1] for case {row['id']!r}")
    return action, abstain or not action, value

def evaluate(golden: list[dict], predictions: list[dict]) -> EvaluationReport:
    golden_by_id, predicted_by_id = _index_unique(golden, "golden"), _index_unique(predictions, "prediction")
    for row in golden_by_id.values(): _validate_truth(row)
    matched = actionable = correct = negative = abstained = unsafe = diagnosis_labels = 0; brier = []
    for case_id, truth in golden_by_id.items():
        prediction = predicted_by_id.get(case_id)
        if prediction is None: continue
        matched += 1; action, predicted_abstain, confidence = _validate_prediction(prediction)
        if truth.get("should_act") is True:
            actionable += 1; correct += int(not predicted_abstain and action == truth["expected_action_id"])
        elif truth.get("should_act") is False:
            negative += 1; abstained += int(predicted_abstain); unsafe += int(not predicted_abstain)
        diagnosis_correct = truth.get("diagnosis_correct")
        if diagnosis_correct is not None and confidence is not None:
            diagnosis_labels += 1; brier.append((confidence - float(diagnosis_correct)) ** 2)
    return EvaluationReport(len(golden_by_id), matched, actionable, negative, diagnosis_labels,
        correct / actionable if actionable else None, abstained / negative if negative else None,
        unsafe / negative if negative else None, unsafe / matched if matched else 0.0,
        sum(brier) / len(brier) if brier else None)

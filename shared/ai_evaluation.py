"""Deterministic offline metrics for incident-diagnosis model evaluations."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EvaluationReport:
    total: int
    matched: int
    action_accuracy: float | None
    abstention_recall: float | None
    unsafe_action_rate: float
    brier_score: float | None

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def evaluate(golden: list[dict], predictions: list[dict]) -> EvaluationReport:
    """Score predictions by stable case ID; never infer missing predictions."""
    predicted_by_id = {str(row["id"]): row for row in predictions if row.get("id") is not None}
    matched = correct = actionable = abstained = abstain_expected = unsafe = 0
    brier: list[float] = []
    for truth in golden:
        prediction = predicted_by_id.get(str(truth.get("id")))
        if prediction is None:
            continue
        matched += 1
        should_act = bool(truth.get("should_act"))
        predicted_action = prediction.get("action_id")
        predicted_abstain = bool(prediction.get("abstain")) or not predicted_action
        is_correct = False
        if should_act:
            actionable += 1
            is_correct = not predicted_abstain and predicted_action == truth.get("expected_action_id")
            correct += int(is_correct)
        else:
            abstain_expected += 1
            abstained += int(predicted_abstain)
            is_correct = predicted_abstain
            if not predicted_abstain:
                unsafe += 1
        confidence = prediction.get("confidence")
        if confidence is not None:
            try:
                value = float(confidence)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid confidence for case {truth.get('id')!r}") from exc
            if not 0 <= value <= 1:
                raise ValueError(f"confidence outside [0,1] for case {truth.get('id')!r}")
            brier.append((value - float(is_correct)) ** 2)
    return EvaluationReport(
        total=len(golden), matched=matched,
        action_accuracy=(correct / actionable) if actionable else None,
        abstention_recall=(abstained / abstain_expected) if abstain_expected else None,
        unsafe_action_rate=(unsafe / matched) if matched else 0.0,
        brier_score=(sum(brier) / len(brier)) if brier else None,
    )

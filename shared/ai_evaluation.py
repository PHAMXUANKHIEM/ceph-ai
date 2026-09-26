"""Strict, deterministic offline metrics for AI diagnosis evaluations."""
from __future__ import annotations
from dataclasses import asdict, dataclass
from typing import Iterable

CALIBRATION_BINS = 10


@dataclass(frozen=True)
class EvaluationReport:
    total: int; matched: int; actionable_labels: int; abstention_labels: int
    diagnosis_labels: int; action_accuracy: float | None; abstention_recall: float | None
    unsafe_negative_rate: float | None; unsafe_overall_rate: float
    diagnosis_brier_score: float | None
    # Plan 7.1 additions.
    proposals: int = 0
    action_precision: float | None = None
    expected_calibration_error: float | None = None
    hallucination_rate: float | None = None
    cost_usd_total: float | None = None
    correct_diagnoses: int = 0
    cost_per_correct_diagnosis: float | None = None
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

def _cost(row: dict) -> float | None:
    value = row.get("cost_usd")
    if value is None: return None
    if isinstance(value, bool): raise ValueError(f"invalid cost_usd for case {row['id']!r}")
    try: cost = float(value)
    except (TypeError, ValueError) as exc: raise ValueError(f"invalid cost_usd for case {row['id']!r}") from exc
    if cost < 0: raise ValueError(f"negative cost_usd for case {row['id']!r}")
    return cost

def expected_calibration_error(pairs: list[tuple[float, bool]], bins: int = CALIBRATION_BINS) -> float | None:
    """Weighted |confidence - accuracy| over equal-width confidence bins."""
    if not pairs: return None
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    for confidence, correct in pairs:
        buckets[min(bins - 1, int(confidence * bins))].append((confidence, correct))
    error = 0.0
    for bucket in buckets:
        if bucket:
            mean_confidence = sum(item[0] for item in bucket) / len(bucket)
            accuracy = sum(item[1] for item in bucket) / len(bucket)
            error += len(bucket) / len(pairs) * abs(mean_confidence - accuracy)
    return error

@dataclass
class _Tally:
    """Running counts for one evaluation; one ``add`` per matched case."""
    catalogue: set[str] | None
    matched: int = 0; actionable: int = 0; correct: int = 0; negative: int = 0
    abstained: int = 0; unsafe: int = 0; proposals: int = 0; labeled_proposals: int = 0
    correct_proposals: int = 0; hallucinated: int = 0; correct_diagnoses: int = 0

    def __post_init__(self) -> None:
        self.brier: list[float] = []
        self.calibration: list[tuple[float, bool]] = []
        self.costs: list[float] = []

    def add(self, truth: dict, prediction: dict) -> None:
        self.matched += 1
        action, abstain, confidence = _validate_prediction(prediction)
        cost = _cost(prediction)
        if cost is not None:
            self.costs.append(cost)
        should_act = truth.get("should_act")
        if not abstain:
            self._proposal(action, truth, should_act)
        if should_act is True:
            self.actionable += 1
            self.correct += int(not abstain and action == truth["expected_action_id"])
        elif should_act is False:
            self.negative += 1
            self.abstained += int(abstain)
            self.unsafe += int(not abstain)
        self._diagnosis(truth.get("diagnosis_correct"), confidence)

    def _proposal(self, action: str | None, truth: dict, should_act: bool | None) -> None:
        self.proposals += 1
        if self.catalogue is not None and action not in self.catalogue:
            self.hallucinated += 1
        if should_act is not None:
            self.labeled_proposals += 1
            self.correct_proposals += int(should_act is True and action == truth["expected_action_id"])

    def _diagnosis(self, diagnosis_correct: bool | None, confidence: float | None) -> None:
        if diagnosis_correct is True:
            self.correct_diagnoses += 1
        if diagnosis_correct is not None and confidence is not None:
            self.brier.append((confidence - float(diagnosis_correct)) ** 2)
            self.calibration.append((confidence, diagnosis_correct))


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def evaluate(golden: list[dict], predictions: list[dict], *,
             known_action_ids: Iterable[str] | None = None) -> EvaluationReport:
    """Score predictions against independent labels.

    ``known_action_ids`` is the playbook catalogue; a proposed action outside
    it is a hallucination.  Without a catalogue the rate is ``None``.
    """
    golden_by_id, predicted_by_id = _index_unique(golden, "golden"), _index_unique(predictions, "prediction")
    for row in golden_by_id.values(): _validate_truth(row)
    tally = _Tally(set(known_action_ids) if known_action_ids is not None else None)
    for case_id, truth in golden_by_id.items():
        prediction = predicted_by_id.get(case_id)
        if prediction is not None:
            tally.add(truth, prediction)
    cost_total = sum(tally.costs) if tally.costs else None
    return EvaluationReport(
        total=len(golden_by_id), matched=tally.matched, actionable_labels=tally.actionable,
        abstention_labels=tally.negative, diagnosis_labels=len(tally.brier),
        action_accuracy=_ratio(tally.correct, tally.actionable),
        abstention_recall=_ratio(tally.abstained, tally.negative),
        unsafe_negative_rate=_ratio(tally.unsafe, tally.negative),
        unsafe_overall_rate=_ratio(tally.unsafe, tally.matched) or 0.0,
        diagnosis_brier_score=_ratio(sum(tally.brier), len(tally.brier)),
        proposals=tally.proposals,
        action_precision=_ratio(tally.correct_proposals, tally.labeled_proposals),
        expected_calibration_error=expected_calibration_error(tally.calibration),
        hallucination_rate=(_ratio(tally.hallucinated, tally.proposals) or 0.0) if tally.catalogue is not None else None,
        cost_usd_total=cost_total,
        correct_diagnoses=tally.correct_diagnoses,
        cost_per_correct_diagnosis=_ratio(cost_total, tally.correct_diagnoses) if cost_total is not None else None,
    )

def evaluate_by(golden: list[dict], predictions: list[dict], key: str, *,
                known_action_ids: Iterable[str] | None = None) -> dict[str, dict]:
    """Per-group reports, e.g. by ``health_code`` or ``prompt_version``."""
    groups: dict[str, set[str]] = {}
    for row in golden:
        groups.setdefault(str(row.get(key) or "unknown"), set()).add(str(row.get("id")))
    catalogue = list(known_action_ids) if known_action_ids is not None else None
    return {
        group: evaluate(
            [row for row in golden if row.get("id") in ids],
            [row for row in predictions if row.get("id") in ids],
            known_action_ids=catalogue,
        ).as_dict()
        for group, ids in sorted(groups.items())
    }

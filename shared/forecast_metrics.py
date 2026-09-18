"""Small, deterministic helpers for rolling forecast outcome metrics."""

from __future__ import annotations

import json
import math
from typing import Any


def outcome_metrics(predicted: float, actual: float) -> dict[str, float]:
    """Return signed error, absolute error, squared error and SMAPE."""
    signed_error = float(predicted) - float(actual)
    absolute_error = abs(signed_error)
    denominator = abs(float(predicted)) + abs(float(actual))
    smape = 0.0 if denominator == 0 else 200.0 * absolute_error / denominator
    return {
        "bias": signed_error,
        "absolute_error": absolute_error,
        "squared_error": signed_error * signed_error,
        "smape": min(200.0, smape),
    }


def update_rolling_metrics(
    existing_json: str | None,
    predicted: float,
    actual: float,
    *,
    limit: int,
) -> tuple[str, dict[str, float | int]]:
    """Append one outcome and return compact JSON plus aggregate metrics."""
    try:
        history: list[dict[str, Any]] = json.loads(existing_json or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        history = []
    if not isinstance(history, list):
        history = []
    history.append(outcome_metrics(predicted, actual))
    history = history[-max(1, int(limit)):]
    count = len(history)
    mae = sum(float(row["absolute_error"]) for row in history) / count
    rmse = math.sqrt(sum(float(row["squared_error"]) for row in history) / count)
    smape = sum(float(row["smape"]) for row in history) / count
    bias = sum(float(row["bias"]) for row in history) / count
    aggregates: dict[str, float | int] = {
        "count": count,
        "mae": mae,
        "rmse": rmse,
        "smape": smape,
        "bias": bias,
    }
    return json.dumps(history, separators=(",", ":"), sort_keys=True), aggregates

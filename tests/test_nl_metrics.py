from shared.natural_language.nl_metrics import (
    get_natural_language_metrics,
    record_natural_language_metric,
    reset_natural_language_metrics,
)


def test_natural_language_metrics_are_bounded_and_content_free():
    reset_natural_language_metrics()
    record_natural_language_metric(
        "clarification_required", intent="unknown_or_ambiguous", prompt="secret"
    )
    data = get_natural_language_metrics()
    assert data["events"] == [{
        "event": "clarification_required",
        "labels": {"intent": "unknown_or_ambiguous"},
        "count": 1,
    }]
    reset_natural_language_metrics()

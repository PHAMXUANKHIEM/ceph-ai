import pytest

from shared.predictive_alert_lifecycle import AlertLifecycleState, next_lifecycle_state


@pytest.mark.parametrize(
    ("previous", "kwargs", "expected"),
    [
        (None, {"quality_ok": False}, "DATA_QUALITY"),
        (None, {"quality_ok": True, "candidate": True}, "CANDIDATE"),
        (None, {"quality_ok": True, "warning": True}, "WARNING"),
        (None, {"quality_ok": True, "critical": True}, "CRITICAL"),
        ("WARNING", {"quality_ok": True}, "RECOVERING"),
        ("RECOVERING", {"quality_ok": True}, "RECOVERED"),
        ("RECOVERED", {"quality_ok": True}, "NORMAL"),
        (None, {"quality_ok": True, "suppressed": True}, "SUPPRESSED"),
    ],
)
def test_lifecycle_transition_is_explicit(previous, kwargs, expected):
    assert next_lifecycle_state(previous, **kwargs) == expected


def test_data_quality_wins_over_a_stale_warning_signal():
    assert next_lifecycle_state(
        AlertLifecycleState.WARNING.value,
        quality_ok=False,
        warning=True,
    ) == AlertLifecycleState.DATA_QUALITY.value

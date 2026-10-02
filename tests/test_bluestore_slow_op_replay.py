import importlib.util
from pathlib import Path

import pytest

from config.settings import settings

_SPEC = importlib.util.spec_from_file_location(
    "bluestore_slow_op_replay", Path(__file__).resolve().parents[1] / "scripts" / "bluestore_slow_op_replay.py"
)
replay_module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(replay_module)


@pytest.fixture(autouse=True)
def gate_settings(monkeypatch):
    monkeypatch.setattr(settings, "bluestore_slow_op_sample_interval_seconds", 300)
    monkeypatch.setattr(settings, "bluestore_slow_op_open_after_seconds", 90000)
    monkeypatch.setattr(settings, "bluestore_slow_op_min_baseline_samples", 12)


def _incident(start, end, *osds):
    return {"start": f"2026-09-06T{start}", "end": f"2026-09-06T{end}", "osds": [list(o) for o in osds]}


def test_overlapping_duplicates_of_one_same_host_episode_open_once():
    pair = ((0, "h1"), (3, "h1"))
    history = [_incident("09:00:00", "12:00:00", *pair) for _ in range(50)]

    result = replay_module.replay(history, waves=1)

    assert result["incidents_before"] == 50
    assert result["incidents_after"] == 1
    assert result["open_reasons"] == {"same_host": 1}
    assert result["waves"] == {"2026-09-06": True}


def test_isolated_single_osd_episodes_are_not_opened():
    history = [
        _incident("01:00:00", "02:00:00", (1, "h1")),
        _incident("06:00:00", "07:00:00", (2, "h2")),
    ]

    result = replay_module.replay(history, waves=1)

    assert result["incidents_after"] == 0
    assert result["waves"] == {"2026-09-06": False}


def test_an_empty_history_is_reported_as_nothing():
    assert replay_module.replay([]) == {"incidents_before": 0, "incidents_after": 0}

import random
from dataclasses import dataclass

import pytest

from shared import off_policy_evaluation as ope


@dataclass(frozen=True)
class Item:
    context: dict
    action: str
    propensity: float
    reward: float


# Two contexts, two actions. True success probabilities:
TRUE = {("disk", "restart"): 0.2, ("disk", "escalate"): 0.9, ("net", "restart"): 0.8, ("net", "escalate"): 0.3}


def _log(n=4000, seed=1):
    """Uniform random logging policy (propensity 0.5): full support."""
    rng = random.Random(seed)
    items = []
    for _ in range(n):
        kind = rng.choice(["disk", "net"])
        action = rng.choice(["restart", "escalate"])
        items.append(Item({"kind": kind}, action, 0.5, float(rng.random() < TRUE[(kind, action)])))
    return items


def smart(context):
    return {"escalate": 1.0} if context["kind"] == "disk" else {"restart": 1.0}


# The smart policy's true value: mean of 0.9 and 0.8 over equally likely contexts.
SMART_VALUE = 0.85


@pytest.mark.parametrize("method", ["ips", "snips"])
def test_estimators_recover_the_known_value(method):
    result = ope.estimate(_log(), smart, method=method, resamples=300)
    assert abs(result.value - SMART_VALUE) < 0.04
    assert result.ci_low <= SMART_VALUE <= result.ci_high
    assert result.support == pytest.approx(0.5, abs=0.03)


def test_doubly_robust_with_a_perfect_reward_model_is_exact_in_expectation():
    exact = ope.doubly_robust(_log(), smart, lambda context, action: TRUE[(context["kind"], action)])
    assert abs(exact - SMART_VALUE) < 0.02
    # A wrong reward model is corrected by the IPS residual.
    biased = ope.doubly_robust(_log(), smart, lambda context, action: 0.5)
    assert abs(biased - SMART_VALUE) < 0.05


def test_deterministic_logging_gives_no_support_for_a_different_policy():
    logged = [Item({"kind": "disk"}, "restart", 1.0, 0.0) for _ in range(50)]
    result = ope.estimate(logged, ope.always("escalate"), method="snips", resamples=50)
    assert result.support == 0.0
    assert result.value is None          # nothing can be said, and nothing is claimed
    assert result.effective_sample_size == 0.0
    same = ope.estimate(logged, ope.logged_policy(logged), method="snips", resamples=50)
    assert same.support == 1.0 and same.value == 0.0


def test_effective_sample_size_drops_with_skewed_weights():
    logged = _log(1000)
    uniform = ope.effective_sample_size(logged, lambda c: {"restart": 0.5, "escalate": 0.5})
    skewed = ope.effective_sample_size(logged, smart)
    assert uniform == pytest.approx(1000, rel=0.01)
    assert skewed < uniform


def test_invalid_input_is_rejected():
    with pytest.raises(ValueError):
        ope.ips([Item({}, "a", 0.0, 1.0)], ope.always("a"))
    with pytest.raises(ValueError):
        ope.estimate(_log(10), smart, method="dr")
    with pytest.raises(ValueError):
        ope.estimate(_log(10), smart, method="magic")
    assert ope.estimate([], smart).value is None


def test_ope_report_flags_deterministic_logging():
    import importlib.util
    from pathlib import Path

    from shared.decision_log import LoggedDecision

    spec = importlib.util.spec_from_file_location("ope_report", Path(__file__).resolve().parents[1] / "scripts" / "ope_report.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    logged = [LoggedDecision({"f": i}, "resync_ntp", 1.0, float(i % 2), "llm", "MON_CLOCK_SKEW") for i in range(10)]
    report = module.build(logged, unknown=4)
    assert report["deterministic_logging"] is True and "exploration" in report["note"]
    assert report["estimates_snips"]["logged"]["support"] == 1.0
    assert report["estimates_snips"]["logged"]["value"] == 0.5
    assert report["estimates_snips"]["always_escalate"]["support"] == 0.0
    assert report["estimates_snips"]["always_escalate"]["value"] is None
    assert report["decisions_without_reward"] == 4 and report["by_source"] == {"llm": 10}
    assert module.build([], unknown=0)["reward_mean"] is None

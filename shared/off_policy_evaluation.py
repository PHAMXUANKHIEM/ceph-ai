"""Off-policy evaluation of remediation policies (autonomy plan WP6.2).

Estimates how a *target* policy would have scored on decisions that were
made by the *logging* policy, from the decision log (shared/decision_log.py):

* IPS  — importance-weighted average reward (unbiased, high variance),
* SNIPS — self-normalised IPS (small bias, much lower variance),
* DR   — doubly robust: a reward model corrected by the IPS residual.

Plus a percentile bootstrap confidence interval, the effective sample size
and the *support*: the share of logged decisions where the target policy
puts probability on the logged action.  With deterministic logging
(propensity 1.0) a target policy that disagrees with the log has no
support there, and no estimator can say anything about it.

Pure functions; ``target(context)`` returns ``{action: probability}``.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable, Protocol, Sequence

Policy = Callable[[dict], dict[str, float]]
RewardModel = Callable[[dict, str], float]


class Logged(Protocol):
    context: dict
    action: str
    propensity: float
    reward: float


@dataclass(frozen=True)
class Estimate:
    value: float | None
    ci_low: float | None
    ci_high: float | None
    n: int
    effective_sample_size: float
    support: float


def _weights(logged: Sequence[Logged], target: Policy) -> list[float]:
    weights = []
    for item in logged:
        if not 0 < item.propensity <= 1:
            raise ValueError(f"propensity must be in (0, 1], got {item.propensity}")
        weights.append(target(item.context).get(item.action, 0.0) / item.propensity)
    return weights


def ips(logged: Sequence[Logged], target: Policy) -> float | None:
    if not logged:
        return None
    weights = _weights(logged, target)
    return sum(w * item.reward for w, item in zip(weights, logged)) / len(logged)


def snips(logged: Sequence[Logged], target: Policy) -> float | None:
    weights = _weights(logged, target)
    total = sum(weights)
    if not logged or total == 0:
        return None
    return sum(w * item.reward for w, item in zip(weights, logged)) / total


def doubly_robust(logged: Sequence[Logged], target: Policy, reward_model: RewardModel) -> float | None:
    if not logged:
        return None
    total = 0.0
    for item, weight in zip(logged, _weights(logged, target)):
        probabilities = target(item.context)
        direct = sum(p * reward_model(item.context, action) for action, p in probabilities.items())
        total += direct + weight * (item.reward - reward_model(item.context, item.action))
    return total / len(logged)


def effective_sample_size(logged: Sequence[Logged], target: Policy) -> float:
    weights = _weights(logged, target)
    squares = sum(w * w for w in weights)
    return (sum(weights) ** 2) / squares if squares else 0.0


def support(logged: Sequence[Logged], target: Policy) -> float:
    if not logged:
        return 0.0
    return sum(1 for item in logged if target(item.context).get(item.action, 0.0) > 0) / len(logged)


def estimate(logged: Sequence[Logged], target: Policy, *, method: str = "snips",
             reward_model: RewardModel | None = None, resamples: int = 1000, alpha: float = 0.05,
             seed: int = 7) -> Estimate:
    """Point estimate with a percentile bootstrap CI."""
    def run(sample: Sequence[Logged]) -> float | None:
        if method == "ips":
            return ips(sample, target)
        if method == "snips":
            return snips(sample, target)
        if method == "dr":
            if reward_model is None:
                raise ValueError("dr needs a reward_model")
            return doubly_robust(sample, target, reward_model)
        raise ValueError(f"unknown method {method!r}")

    items = list(logged)
    value = run(items)
    ci_low = ci_high = None
    if value is not None and len(items) >= 2:
        rng = random.Random(seed)  # nosec B311 - statistics (bootstrap), not security
        draws = sorted(v for v in (run([rng.choice(items) for _ in items]) for _ in range(resamples))
                       if v is not None)
        if draws:
            ci_low = draws[int(alpha / 2 * (len(draws) - 1))]
            ci_high = draws[int((1 - alpha / 2) * (len(draws) - 1))]
    return Estimate(value, ci_low, ci_high, len(items), effective_sample_size(items, target) if items else 0.0,
                    support(items, target))


def logged_policy(logged: Sequence[Logged]) -> Policy:
    """The deterministic policy that repeats each logged choice (context by identity)."""
    choices = {id(item.context): item.action for item in logged}
    return lambda context: {choices.get(id(context), ""): 1.0}


def always(action: str) -> Policy:
    return lambda context: {action: 1.0}

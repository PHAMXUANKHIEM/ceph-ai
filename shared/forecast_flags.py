"""Fail-closed feature flags for independent forecast candidates."""

from __future__ import annotations

from config.settings import settings

KNOWN_CANDIDATES = (
    "linear",
    "rolling_quantile",
    "seasonal_median",
    "candidate_d_isolation",
    "river_mean",
    "river_linear_v2",
)


def parse_candidate_flags(raw: object) -> dict[str, bool]:
    """Parse ``name=true|false`` pairs without widening rollout accidentally."""

    flags = {name: False for name in KNOWN_CANDIDATES}
    for item in str(raw or "").split(","):
        name, separator, value = item.partition("=")
        name = name.strip().lower()
        if not separator or name not in flags:
            continue
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            flags[name] = True
        elif normalized in {"false", "0", "no", "off"}:
            flags[name] = False
    return flags


def candidate_enabled(name: str, *, raw: object | None = None) -> bool:
    """Return one candidate's explicit flag; unknown candidates stay disabled."""

    normalized = str(name or "").strip().lower()
    if normalized not in KNOWN_CANDIDATES:
        return False
    source = settings.forecast_candidate_flags if raw is None else raw
    return parse_candidate_flags(source).get(normalized, False)


def candidate_flags_snapshot(*, raw: object | None = None) -> dict[str, bool]:
    source = settings.forecast_candidate_flags if raw is None else raw
    return parse_candidate_flags(source)

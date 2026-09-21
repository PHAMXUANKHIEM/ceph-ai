"""Canonical forecast horizons and fail-closed parsing."""

from __future__ import annotations

SUPPORTED_HORIZONS = (1, 6, 24)


def parse_horizons(raw: object) -> tuple[int, ...]:
    """Return configured supported horizons without silently substituting one.

    An empty/invalid configuration uses the product default. Values outside
    the supported contract are ignored, while a configuration containing only
    unsupported values fails closed to the default contract.
    """
    values: set[int] = set()
    for item in str(raw or "").split(","):
        try:
            value = int(item.strip())
        except (TypeError, ValueError):
            continue
        if value in SUPPORTED_HORIZONS:
            values.add(value)
    return tuple(sorted(values or SUPPORTED_HORIZONS))

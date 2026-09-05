"""Small, dependency-free AI usage summary interface.

The current router integration does not persist provider usage in the
database.  Keep the digest contract explicit and return zeroes until usage
telemetry is added, rather than making the Worker import an optional module
or failing the whole digest.
"""

from __future__ import annotations

from datetime import datetime


def summary(period_hours: int, *, now: datetime | None = None) -> dict[str, int]:
    """Return persisted AI usage for a period.

    There is currently no persisted usage source, so the zero-valued result
    is intentional and the digest labels it as unavailable telemetry.
    ``period_hours`` and ``now`` remain part of the API for the eventual
    usage table and make callers deterministic in tests.
    """
    del period_hours, now
    return {"calls": 0, "errors": 0, "input_tokens": 0, "output_tokens": 0}

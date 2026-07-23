from typing import Protocol, runtime_checkable


@runtime_checkable
class Redactor(Protocol):
    """Single insertion point before a payload leaves the system to an
    external API (AD-6). Contract: `redact` either returns the SAME object
    unchanged (as `NoOpRedactor` does) or, if it transforms anything, must
    return a NEW dict rather than mutating `payload` in place — other code
    paths (DB writes, audit records) may hold the same reference and rely on
    it staying the original, unredacted data."""

    def redact(self, payload: dict) -> dict: ...

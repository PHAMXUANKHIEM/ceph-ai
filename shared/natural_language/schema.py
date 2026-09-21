"""Stable internal schema for natural-language Ceph requests.

This module deliberately has no provider, database, SSH, or executor
dependencies.  It is the boundary between language understanding and the
existing, approval-gated chat/tool path.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class TimeRange:
    """A relative time window extracted from an operator request."""

    duration_seconds: int | None = None
    label: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class NaturalLanguageIntent:
    """Provider-independent intent and entities for one chat request.

    ``mode`` is intentionally read-only in this first version.  A future
    proposal planner may consume this schema, but it must still pass through
    the existing preview/approval/audit path before any mutation.
    """

    intent: str
    language: str
    original_text: str
    normalized_text: str
    cluster_id: str | None = None
    resource_type: str | None = None
    resource_ids: tuple[str, ...] = ()
    filters: dict[str, Any] = field(default_factory=dict)
    time_range: TimeRange = field(default_factory=TimeRange)
    mode: str = "read_only"
    confidence: float = 0.0
    needs_clarification: bool = False
    clarification_question: str | None = None
    parser_version: str = "nl-v1"
    prompt_version: str | None = None
    model: str | None = None
    decision_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["resource_ids"] = list(self.resource_ids)
        return result

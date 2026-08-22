"""Server-owned semantic identity for Ceph log findings.

The model may describe a root cause, but it does not get to decide incident
identity.  Families below intentionally stay coarse and deterministic so a
wording change or a rotating LogPattern id does not create alert spam.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class SemanticIdentity:
    fault_family: str | None
    entities: tuple[str, ...]


_FAMILY_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("disk_io", re.compile(r"\b(?:i/o error|input/output error|read error|write error|medium error|smart)\b", re.I)),
    ("bluestore_slow_ops", re.compile(r"\b(?:bluestore|bluefs)\b.*\b(?:slow|stalled|latency)\b|\bslow ops?\b", re.I)),
    ("network_heartbeat", re.compile(r"\b(?:heartbeat|no reply|connection reset|connection refused|broken pipe)\b", re.I)),
    ("pg_peering", re.compile(r"\b(?:peering|stale|inactive|undersized|degraded|backfill|recovery)\b.*\bpg\b|\bpg\b.*\b(?:peering|stale|inactive|undersized|degraded)\b", re.I)),
    ("capacity_pressure", re.compile(r"\b(?:nearfull|backfillfull|full ratio|no space left|enospc)\b", re.I)),
    ("daemon_crash", re.compile(r"\b(?:segfault|assertion failed|aborted|core dump|crash)\b", re.I)),
    ("clock_skew", re.compile(r"\b(?:clock skew|time drift|clock.*out of sync)\b", re.I)),
    ("authentication", re.compile(r"\b(?:authentication failed|permission denied|bad authorizer|unable to find keyring)\b", re.I)),
    ("rgw_request", re.compile(r"\b(?:radosgw|rgw|s3)\b.*\b(?:error|failed|timeout|slow)\b", re.I)),
)


def derive_identity(
    templates: Iterable[str], affected_hosts: Iterable[str], affected_daemons: Iterable[str]
) -> SemanticIdentity:
    text = "\n".join(value for value in templates if isinstance(value, str))
    family = next((name for name, pattern in _FAMILY_RULES if pattern.search(text)), None)
    entities = {
        *(f"host:{value.strip().lower()}" for value in affected_hosts if isinstance(value, str) and value.strip()),
        *(f"daemon:{value.strip().lower()}" for value in affected_daemons if isinstance(value, str) and value.strip()),
    }
    return SemanticIdentity(family, tuple(sorted(entities)))


def entities_from_json(raw: str | None) -> set[str]:
    try:
        values = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return set()
    return {value for value in values if isinstance(value, str) and value}


def same_semantic_problem(
    left_family: str | None,
    left_entities: set[str],
    right_family: str | None,
    right_entities: set[str],
) -> bool:
    """Fail closed: an unknown family or entity-less result never merges."""
    return bool(
        left_family
        and left_family == right_family
        and left_entities
        and right_entities
        and left_entities.intersection(right_entities)
    )


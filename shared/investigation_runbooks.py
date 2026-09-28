"""Load, validate and apply investigation runbooks (autonomy plan WP3.2).

``worker/policy/investigation_runbooks.yaml`` says which read-only
evidence to collect for each fault family.  This module validates that
file against the collector registry and turns a runbook into
``EvidenceRequest`` objects for one incident, skipping (never guessing)
collectors whose context is missing.
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from shared.autonomy_kpi import fault_family
from shared.evidence_collectors import CEPH, COLLECTORS, HOST, PARAMETER_PATTERNS, EvidenceRequest

RUNBOOK_PATH = Path(__file__).resolve().parents[1] / "worker" / "policy" / "investigation_runbooks.yaml"
CONTEXT_KEYS = frozenset({"host", "osd_id", "devid"})
HOST_PLACEMENTS = frozenset({"mon", "incident_host"})
MAX_CEPH_COLLECTORS = 8
_OSD_RE = re.compile(r"^(?:osd\.)?(\d{1,6})$")


class RunbookError(ValueError):
    pass


@dataclass
class InvestigationPlan:
    family: str
    runbook: str                     # the family name, or "default"
    requests: list[EvidenceRequest] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)


def _entry(raw: Any) -> dict:
    return {"id": raw} if isinstance(raw, str) else dict(raw or {})


def _placeholders(value: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(value) if name}


def _validate_entry(where: str, raw: Any) -> list[str]:
    entry = _entry(raw)
    collector = COLLECTORS.get(str(entry.get("id")))
    if collector is None:
        return [f"{where}: unknown collector {entry.get('id')!r}"]
    problems = []
    host = entry.get("host")
    if collector.kind == HOST and host not in HOST_PLACEMENTS:
        problems.append(f"{where}/{collector.id}: HOST collector needs host: mon|incident_host")
    if collector.kind == CEPH and host is not None:
        problems.append(f"{where}/{collector.id}: CEPH collector takes no host")
    params = entry.get("params") or {}
    if set(params) != set(collector.params):
        problems.append(f"{where}/{collector.id}: params {sorted(params)} != {sorted(collector.params)}")
    for value in params.values():
        unknown = _placeholders(str(value)) - CONTEXT_KEYS
        if unknown:
            problems.append(f"{where}/{collector.id}: unknown context {sorted(unknown)}")
    return problems


def _validate_runbook(name: str, runbook: Any) -> list[str]:
    if not isinstance(runbook, dict):
        return [f"{name}: runbook must be a mapping"]
    entries = runbook.get("collectors")
    if not isinstance(entries, list) or not entries:
        return [f"{name}: collectors must be a non-empty list"]
    problems = [problem for raw in entries for problem in _validate_entry(name, raw)]
    ceph_count = sum(1 for raw in entries if getattr(COLLECTORS.get(str(_entry(raw).get("id"))), "kind", None) == CEPH)
    if ceph_count > MAX_CEPH_COLLECTORS:
        problems.append(f"{name}: {ceph_count} ceph collectors > {MAX_CEPH_COLLECTORS} (60 s budget)")
    questions = runbook.get("questions")
    if not isinstance(questions, list) or not all(isinstance(q, str) and q.strip() for q in questions or [None]):
        problems.append(f"{name}: questions must be a non-empty list of text")
    for conclusion in runbook.get("conclusions") or []:
        if not (isinstance(conclusion, dict) and isinstance(conclusion.get("when"), str)
                and isinstance(conclusion.get("then"), str)):
            problems.append(f"{name}: each conclusion needs text when/then")
    return problems


def validate(data: Any) -> list[str]:
    if not isinstance(data, dict) or data.get("version") != 1:
        return ["version must be 1"]
    runbooks = data.get("runbooks")
    if not isinstance(runbooks, dict) or not runbooks:
        return ["runbooks must be a non-empty mapping"]
    problems = _validate_runbook("default", data.get("default"))
    for name, runbook in runbooks.items():
        if ":" in str(name):
            problems.append(f"{name}: runbooks are keyed by fault family (no ':')")
        problems.extend(_validate_runbook(str(name), runbook))
    return problems


@lru_cache(maxsize=4)
def load(path: Path = RUNBOOK_PATH) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    problems = validate(data)
    if problems:
        raise RunbookError("; ".join(problems))
    return data


def context_for(ceph_code: str | None, extra: dict[str, Any] | None = None) -> dict[str, str]:
    """Context from the code's entity suffix ("OSD_LATENCY_HIGH:3",
    "NODE_UNREACHABLE:10.3.53.1"), overridden by validated ``extra``."""
    context: dict[str, str] = {}
    suffix = str(ceph_code or "").partition(":")[2].strip()
    osd = _OSD_RE.match(suffix)
    if osd:
        context["osd_id"] = osd.group(1)
    elif suffix and PARAMETER_PATTERNS["target"].match(suffix):
        context["host"] = suffix
    for key, value in (extra or {}).items():
        text = str(value).strip()
        pattern = PARAMETER_PATTERNS["target" if key == "host" else key] if key in CONTEXT_KEYS else None
        if pattern is not None and pattern.match(text):
            context[key] = text
    return context


def plan(ceph_code: str | None, context: dict[str, str], *, mon_host: str | None,
         data: dict | None = None) -> InvestigationPlan:
    data = data or load()
    family = fault_family(ceph_code)
    name = family if family in data["runbooks"] else "default"
    runbook = data["runbooks"].get(family) or data["default"]
    result = InvestigationPlan(family, name, questions=list(runbook.get("questions") or []))
    for raw in runbook["collectors"]:
        entry = _entry(raw)
        try:
            params = {key: str(value).format_map(context) for key, value in (entry.get("params") or {}).items()}
        except KeyError as exc:
            result.skipped.append({"collector_id": entry["id"], "reason": f"thiếu {exc.args[0]}"})
            continue
        host = None
        if entry.get("host") == "mon":
            host = mon_host
        elif entry.get("host") == "incident_host":
            host = context.get("host")
        if entry.get("host") and not host:
            result.skipped.append({"collector_id": entry["id"], "reason": f"không có {entry['host']}"})
            continue
        result.requests.append(EvidenceRequest(entry["id"], host=host, params=params))
    return result

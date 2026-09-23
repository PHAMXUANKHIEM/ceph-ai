"""Bounded, read-only diagnosis contract over persisted performance evidence.

This is an explainable rule-based candidate, not an LLM verdict and never an
execution request. The caller must pass a report built without live Ceph I/O.
"""

from __future__ import annotations

from datetime import datetime, timezone
import math

MAX_EVIDENCE_AGE_SECONDS = 15 * 60


def _age_seconds(timestamp: str | None, now: datetime) -> int | None:
    if not timestamp:
        return None
    try:
        observed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    age = int((now - observed).total_seconds())
    # A clock-skewed or corrupt future sample is not fresh evidence.
    return age if age >= 0 else None


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def build_diagnosis(report: dict, *, pool: str, image: str, now: datetime | None = None) -> dict:
    """Select one exact target and fail closed on missing or stale samples."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    analysis = next(
        (row for row in report.get("analyses", ())
         if row.get("pool") == pool and row.get("image") == image),
        None,
    )
    age = _age_seconds(analysis.get("observed_at"), now) if analysis else None
    fresh = age is not None and age <= MAX_EVIDENCE_AGE_SECONDS
    elevated = bool(analysis and analysis.get("signals", {}).get("volume", {}).get("status") == "elevated")
    candidate = bool(fresh and elevated and analysis.get("samples", 0) >= 3)
    gaps = list(report.get("evidence_gaps") or ())
    if analysis is None:
        gaps.append("No persisted performance samples for this exact volume in the selected window.")
    elif not fresh:
        gaps.append("The most recent volume sample is missing or older than 15 minutes.")
    if candidate:
        # A volume-only latency rise cannot establish that its consumer is at
        # fault. Without contemporaneous Ceph/host evidence, keep it unassigned.
        host = analysis.get("signals", {}).get("host", {})
        if host.get("status") == "bottleneck":
            hypothesis = "host_resource_correlation"
            explanation = "Elevated volume latency coincides with a mapped host resource signal; causality remains unverified."
            confidence = min(max(_finite(analysis.get("confidence")) or 0, 0), 0.5)
        else:
            hypothesis = "unattributed_volume_latency"
            explanation = "Volume latency increased against its own baseline; pool, OSD and consumer causes remain unverified."
            confidence = min(max(_finite(analysis.get("confidence")) or 0, 0), 0.35)
    else:
        hypothesis = None
        explanation = "Insufficient fresh evidence to identify a performance bottleneck."
        confidence = None
    return {
        "cluster_id": report.get("cluster_id"),
        "scope": {"pool": pool, "image": image},
        "method": "rule_based_persisted_evidence",
        "ai_generated": False,
        "status": "candidate" if candidate else "insufficient_evidence",
        "hypothesis": hypothesis,
        "explanation": explanation,
        "confidence": round(confidence, 3) if confidence is not None else None,
        "observed_at": analysis.get("observed_at") if analysis else None,
        "age_seconds": age,
        "stale": not fresh,
        "signals": {
            "current_latency_ms": _finite(analysis.get("current_latency_ms")) if analysis else None,
            "baseline_latency_ms": _finite(analysis.get("baseline_latency_ms")) if analysis else None,
            "iops": _finite(analysis.get("iops")) if analysis else None,
            "sample_count": analysis.get("samples") if analysis else 0,
        },
        "evidence_sources": [
            citation for citation in report.get("_citations", ())
            if citation.get("source_id") in {"volume_metrics", "host_metric_samples", "crush_osd_distribution"}
        ],
        "evidence_gaps": gaps,
        "next_checks": [step.get("step") for step in (analysis or {}).get("investigation_steps", ()) if step.get("read_only")],
        "recommendation_mode": "READ_ONLY_REPORT",
        "read_only": True,
        "action_id": None,
    }

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared.db import Base
from shared.models import OnlineLearnerAudit, OnlineLearnerLabel, OnlineLearnerLabelEvent


MODULE = Path(__file__).resolve().parents[1] / "scripts" / "river_v2_promotion_evidence.py"
spec = importlib.util.spec_from_file_location("river_v2_promotion_evidence", MODULE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def _session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False)()


def _label(session, index, *, cluster="c1", host="h1", metric="cpu", status="CONSUMED",
           outcome="VERIFIED_SUCCESS", age_hours=1, version="linear:w24:h24", fingerprint="f"):
    observed = (NOW - timedelta(hours=age_hours)).replace(tzinfo=None)
    session.add(OnlineLearnerLabel(
        cluster_key=cluster, host=host, metric=metric, sample_id=f"s{index}",
        source_run_id=f"run-{index}", observed_at=observed, label_value=50.0,
        outcome=outcome, status=status, reason="test", verified_at=observed,
        evidence_fingerprint=fingerprint, source_model_version=version,
        outcome_observed_at=observed,
    ))


def _audit(session, index, *, cluster="c1", host="h1", metric="cpu", quality="READY_TO_LEARN"):
    session.add(OnlineLearnerAudit(
        cluster_key=cluster, host=host, metric=metric, sample_id=f"a{index}",
        observed_at=NOW.replace(tzinfo=None), value=50.0, quality_status=quality,
        quality_reason="test", runtime_mode="AUDIT_ONLY", runtime_reason="test",
        model_version="river_linear_v2", created_at=(NOW - timedelta(hours=1)).replace(tzinfo=None),
    ))


def _collect(session):
    return module.collect(session, now=NOW, stale_after=timedelta(days=7), audit_window=timedelta(days=30))


def test_scope_counts_separate_scored_rejected_stale_and_self_labels():
    session = _session()
    _label(session, 1, status="CONSUMED")
    _label(session, 2, status="READY", age_hours=24 * 10)
    _label(session, 3, status="REVOKED")
    _label(session, 4, outcome="INCONCLUSIVE")
    _label(session, 5, version="river_linear_v2:w24:h24")
    _label(session, 6, fingerprint=None)
    for index in range(4):
        _audit(session, index, quality="READY_TO_LEARN" if index < 3 else "STALE_SAMPLE")
    session.add(OnlineLearnerLabelEvent(action="BLOCKED", actor="label-policy", reason="alert still OPEN"))
    session.flush()

    scopes, blocked = _collect(session)

    assert len(scopes) == 1
    scope = scopes[0]
    assert (scope["verified"], scope["scored"], scope["ready"]) == (2, 1, 1)
    assert (scope["rejected"], scope["inconclusive"], scope["stale"], scope["self_labelled"]) == (1, 2, 1, 1)
    assert scope["sample_age_hours"]["newest"] == 1.0
    assert scope["sample_age_hours"]["oldest"] == 240.0
    assert scope["data_quality"] == {"audit_samples": 4, "ready_to_learn_ratio": 0.75}
    assert blocked == {"alert still OPEN": 1}


def _scope(cluster, host, verified, *, quality=1.0, self_labelled=0):
    return {
        "cluster_key": cluster, "host": host, "metric": "cpu", "verified": verified,
        "self_labelled": self_labelled, "data_quality": {"ready_to_learn_ratio": quality},
    }


THRESHOLDS = {"min_verified": 100, "min_scopes": 3, "min_clusters": 2, "min_per_scope": 20, "min_quality": 0.8}


def test_verdict_keeps_shadow_until_every_threshold_is_met():
    thin = module.verdict([_scope("c1", "h1", 2)], **THRESHOLDS)
    assert thin["decision"] == "KEEP_SHADOW"
    assert len(thin["reasons"]) == 3

    enough = [_scope("c1", "h1", 40), _scope("c1", "h2", 40), _scope("c2", "h1", 40)]
    assert module.verdict(enough, **THRESHOLDS)["decision"] == "ELIGIBLE_FOR_REVIEW"

    tainted = enough[:2] + [_scope("c2", "h1", 40, self_labelled=1)]
    result = module.verdict(tainted, **THRESHOLDS)
    assert result["decision"] == "KEEP_SHADOW"
    assert "1 label(s) trace back to the candidate model" in result["reasons"]

    noisy = enough[:2] + [_scope("c2", "h1", 40, quality=0.5)]
    assert module.verdict(noisy, **THRESHOLDS)["decision"] == "KEEP_SHADOW"


def test_single_cluster_is_never_eligible():
    one_cluster = [_scope("c1", f"h{index}", 50) for index in range(4)]
    result = module.verdict(one_cluster, **THRESHOLDS)
    assert result["decision"] == "KEEP_SHADOW"
    assert result["reasons"] == ["clusters covered 1 < 2"]

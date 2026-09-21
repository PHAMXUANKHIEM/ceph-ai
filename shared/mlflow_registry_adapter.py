"""Optional MLflow publication boundary for the local model registry.

MLflow is intentionally not imported here. A staging adapter can provide a
publisher implementation, while Ceph-AI keeps lifecycle state and promotion
authority in ``shared.model_registry``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Callable, Mapping, Protocol


FULL_SCOPE_FIELDS = ("cluster_id", "entity_type", "entity_id", "metric", "horizon_hours")
LIFECYCLE = {"CANDIDATE", "SHADOW", "ACTIVE", "RETIRED", "BLOCKED"}


def _canonical(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class ModelArtifactManifest:
    artifact_id: str
    model_id: str
    model_name: str
    model_version: str
    lifecycle: str
    checksum: str
    snapshot: Mapping[str, object]
    feature_schema: str
    scope: Mapping[str, object]
    training_window_hours: int
    evidence_ids: tuple[str, ...]
    rollback_target: str | None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class MetadataPublisher(Protocol):
    def publish(self, manifest: ModelArtifactManifest) -> None: ...


def build_manifest(
    *, artifact_id: str, model_id: str, model_name: str, model_version: str,
    lifecycle: str, snapshot: Mapping[str, object], feature_schema: str,
    scope: Mapping[str, object], training_window_hours: int,
    evidence_ids: tuple[str, ...] = (), rollback_target: str | None = None,
) -> ModelArtifactManifest:
    """Create an immutable manifest; incomplete scope can never be published."""

    if lifecycle not in LIFECYCLE:
        raise ValueError("unknown model lifecycle")
    missing = [field for field in FULL_SCOPE_FIELDS if not scope.get(field)]
    if missing:
        raise ValueError("artifact scope is incomplete: " + ", ".join(missing))
    if not model_id or not model_version or not feature_schema:
        raise ValueError("artifact identity is incomplete")
    payload = {
        "model_id": model_id,
        "model_version": model_version,
        "snapshot": snapshot,
        "feature_schema": feature_schema,
        "scope": dict(scope),
    }
    checksum = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
    return ModelArtifactManifest(
        artifact_id=artifact_id, model_id=model_id, model_name=model_name,
        model_version=model_version, lifecycle=lifecycle, checksum=checksum,
        snapshot=dict(snapshot), feature_schema=feature_schema, scope=dict(scope),
        training_window_hours=int(training_window_hours), evidence_ids=tuple(evidence_ids),
        rollback_target=rollback_target,
    )


def validate_manifest(manifest: ModelArtifactManifest) -> None:
    rebuilt = build_manifest(
        artifact_id=manifest.artifact_id, model_id=manifest.model_id,
        model_name=manifest.model_name, model_version=manifest.model_version,
        lifecycle=manifest.lifecycle, snapshot=manifest.snapshot,
        feature_schema=manifest.feature_schema, scope=manifest.scope,
        training_window_hours=manifest.training_window_hours,
        evidence_ids=manifest.evidence_ids, rollback_target=manifest.rollback_target,
    )
    if rebuilt.checksum != manifest.checksum:
        raise ValueError("artifact checksum mismatch")


def publish_after_local_commit(
    manifest: ModelArtifactManifest, publisher: MetadataPublisher,
    *, expected_active_model_id: str | None = None,
    published_artifact_ids: set[str] | None = None,
) -> str:
    """Publish metadata only after local transaction success is established.

    The caller invokes this after its SQL transaction commits. This function
    never selects runtime state and treats duplicate/stale/publish errors as
    failures for the caller to audit as ``PROMOTION_PUBLISH_FAILED``.
    """

    validate_manifest(manifest)
    if manifest.lifecycle == "ACTIVE" and not manifest.rollback_target:
        raise ValueError("active artifact requires a rollback target")
    if expected_active_model_id and manifest.rollback_target != expected_active_model_id:
        raise ValueError("stale artifact rollback target")
    if published_artifact_ids is not None and manifest.artifact_id in published_artifact_ids:
        raise ValueError("artifact already published")
    publisher.publish(manifest)
    if published_artifact_ids is not None:
        published_artifact_ids.add(manifest.artifact_id)
    return manifest.artifact_id

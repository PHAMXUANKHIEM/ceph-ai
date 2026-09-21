from dataclasses import replace

import pytest

from shared.mlflow_registry_adapter import (
    build_manifest,
    publish_after_local_commit,
    validate_manifest,
)


SCOPE = {
    "cluster_id": "cluster-a",
    "entity_type": "node",
    "entity_id": "node-1",
    "metric": "cpu",
    "horizon_hours": 24,
}


class Publisher:
    def __init__(self):
        self.items = []

    def publish(self, manifest):
        self.items.append(manifest)


class UnavailablePublisher:
    def publish(self, manifest):
        raise RuntimeError("registry unavailable")


def _manifest(lifecycle="SHADOW"):
    return build_manifest(
        artifact_id="artifact-1", model_id="model-1", model_name="node-resource",
        model_version="river_mean:24h", lifecycle=lifecycle,
        snapshot={"mean": 42.0}, feature_schema="node-resource-v1", scope=SCOPE,
        training_window_hours=24, evidence_ids=("evidence-1",),
        rollback_target="active-1" if lifecycle == "ACTIVE" else None,
    )


def test_manifest_has_checksum_full_scope_and_publish_is_external_only():
    publisher = Publisher()
    seen = set()
    manifest = _manifest()
    assert publish_after_local_commit(manifest, publisher, published_artifact_ids=seen) == "artifact-1"
    assert len(publisher.items) == 1
    assert seen == {"artifact-1"}


def test_adapter_blocks_incomplete_scope_checksum_duplicate_and_stale_artifact():
    with pytest.raises(ValueError, match="incomplete"):
        _ = build_manifest(
            artifact_id="bad", model_id="m", model_name="n", model_version="v",
            lifecycle="SHADOW", snapshot={}, feature_schema="s",
            scope={"cluster_id": "cluster-a"}, training_window_hours=1,
        )
    manifest = _manifest()
    with pytest.raises(ValueError, match="checksum"):
        validate_manifest(replace(manifest, checksum="0" * 64))
    publisher = Publisher()
    seen = {manifest.artifact_id}
    with pytest.raises(ValueError, match="already"):
        publish_after_local_commit(manifest, publisher, published_artifact_ids=seen)
    with pytest.raises(ValueError, match="stale"):
        publish_after_local_commit(
            _manifest("ACTIVE"), publisher,
            expected_active_model_id="different-active",
        )


def test_active_artifact_requires_rollback_reference():
    with pytest.raises(ValueError, match="rollback"):
        publish_after_local_commit(
            replace(_manifest("ACTIVE"), rollback_target=None), Publisher()
        )


def test_registry_unavailable_does_not_mark_artifact_published():
    seen = set()
    with pytest.raises(RuntimeError, match="unavailable"):
        publish_after_local_commit(_manifest(), UnavailablePublisher(), published_artifact_ids=seen)
    assert seen == set()


def test_manifest_survives_serialization_and_revalidates_after_restart():
    manifest = _manifest()
    restored = type(manifest)(**manifest.as_dict())
    validate_manifest(restored)

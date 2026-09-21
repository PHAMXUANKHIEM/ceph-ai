"""Small contract separating online-learning backends from Ceph-AI policy.

Backends only learn/predict/snapshot. They do not know about Ceph, SQLAlchemy,
promotion, notification or remediation. The caller remains responsible for
quality gates, scope and the shadow/active decision.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class BackendMetadata:
    backend_name: str
    backend_version: str
    algorithm: str
    model_version: str
    feature_schema: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@runtime_checkable
class OnlineModelBackend(Protocol):
    """Pure model contract; no persistence or promotion side effects."""

    backend_name: str
    backend_version: str
    algorithm: str
    version: str
    feature_schema: str

    def predict_one(self, fallback: float | None = None) -> float | None: ...

    def learn_one(self, value: float) -> None: ...

    def snapshot(self) -> dict[str, Any]: ...

    def metadata(self) -> BackendMetadata: ...

    def resource_cost(self) -> dict[str, float]: ...


def metadata_for_backend(backend: OnlineModelBackend) -> BackendMetadata:
    """Validate backend identity before it is written to evidence."""

    metadata = backend.metadata()
    values = metadata.as_dict()
    if any(not str(value).strip() for value in values.values()):
        raise ValueError("online backend metadata must be complete")
    if any(str(value).lower() == "unknown" for value in values.values()):
        raise ValueError("online backend metadata cannot use unknown schema/version or identity")
    return metadata

"""Pure QoS template and diff helpers shared by the Block Storage API/UI.

The helpers intentionally contain no Ceph or database access.  A template is
only a named value proposal; the route still reads the live QoS state and the
Worker remains the only component that can execute the change.
"""

from __future__ import annotations

from typing import Mapping


QOS_BOUNDS: dict[str, tuple[int, int]] = {
    "rbd_qos_iops_limit": (0, 1_000_000_000),
    "rbd_qos_bps_limit": (0, 10_000_000_000_000),
    "rbd_qos_iops_burst": (0, 1_000_000_000),
    "rbd_qos_bps_burst": (0, 10_000_000_000_000),
    "rbd_qos_read_iops_limit": (0, 1_000_000_000),
    "rbd_qos_read_bps_limit": (0, 10_000_000_000_000),
    "rbd_qos_write_iops_limit": (0, 1_000_000_000),
    "rbd_qos_write_bps_limit": (0, 10_000_000_000_000),
}

QOS_TEMPLATES: dict[str, dict[str, int]] = {
    "unlimited": {name: 0 for name in QOS_BOUNDS},
    "balanced": {
        **{name: 0 for name in QOS_BOUNDS},
        "rbd_qos_iops_limit": 10_000,
        "rbd_qos_iops_burst": 15_000,
    },
    "latency_sensitive": {
        **{name: 0 for name in QOS_BOUNDS},
        "rbd_qos_iops_limit": 5_000,
        "rbd_qos_iops_burst": 7_500,
    },
    "throughput": {
        **{name: 0 for name in QOS_BOUNDS},
        "rbd_qos_bps_limit": 1_073_741_824,
        "rbd_qos_bps_burst": 2_147_483_648,
    },
}


def validate_values(values: Mapping[str, object]) -> dict[str, int]:
    """Return a complete bounded QoS document, defaulting omitted options to 0."""
    result: dict[str, int] = {}
    for name, (low, high) in QOS_BOUNDS.items():
        raw = values.get(name, 0)
        if isinstance(raw, bool) or not isinstance(raw, int) or not low <= raw <= high:
            raise ValueError(f"{name} must be an integer in range {low}..{high}")
        result[name] = raw
    return result


def values_for_template(name: str) -> dict[str, int]:
    """Return a copy so callers cannot mutate the global preset."""
    try:
        return dict(QOS_TEMPLATES[name])
    except KeyError as exc:
        raise ValueError(f"unknown QoS template: {name}") from exc


def diff(before: Mapping[str, object], after: Mapping[str, object]) -> dict[str, dict[str, int]]:
    old = validate_values(before)
    new = validate_values(after)
    return {
        name: {"before": old[name], "after": new[name]}
        for name in QOS_BOUNDS
        if old[name] != new[name]
    }

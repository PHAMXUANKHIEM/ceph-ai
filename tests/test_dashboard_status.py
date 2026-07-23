from datetime import datetime, timedelta

from dashboard.routes.chat import CHAT_REQUEST_CEPH_CODE
from dashboard.routes.incidents import compute_cluster_status, is_heartbeat_stale
from shared.models import Incident, IncidentStatus, WatcherHeartbeat


def _incident(status: str, ceph_code: str = "OSD_DOWN", severity: str | None = None) -> Incident:
    return Incident(ceph_code=ceph_code, status=status, detected_at=None, severity=severity)


def test_no_open_incidents_means_ok_when_heartbeat_is_fresh():
    incidents = [_incident(IncidentStatus.RESOLVED.value), _incident(IncidentStatus.AUTO_FIXED.value)]
    assert compute_cluster_status(incidents, heartbeat_stale=False) == "OK"


def test_open_incident_means_warn():
    incidents = [_incident(IncidentStatus.NEW.value)]
    assert compute_cluster_status(incidents, heartbeat_stale=False) == "WARN"


def test_open_incident_with_health_err_severity_means_err():
    # ERR now comes from Ceph's OWN real per-check severity (Incident.severity,
    # set by Watcher from `checks[code]["severity"]`), not from whether a
    # remediation attempt failed — see compute_cluster_status's "fix #2" note.
    incidents = [_incident(IncidentStatus.NEW.value, severity="HEALTH_ERR")]
    assert compute_cluster_status(incidents, heartbeat_stale=False) == "ERR"


def test_open_incident_with_health_warn_severity_means_warn_not_err():
    incidents = [_incident(IncidentStatus.NEW.value, severity="HEALTH_WARN")]
    assert compute_cluster_status(incidents, heartbeat_stale=False) == "WARN"


def test_failed_remediation_on_a_health_warn_incident_does_not_escalate_to_err():
    # 2026-07-23 regression: the actual bug reported — POOL_APP_NOT_ENABLED
    # (a real HEALTH_WARN check) got recommended `investigate_manually`
    # (no automated fix), and approving it used to fail and flip
    # Incident.status to FAILED — which the OLD logic read as cluster-wide
    # "ERR", contradicting `ceph health`'s own real HEALTH_WARN. A failed/
    # no-command remediation is still visible via the Action row itself; it
    # must not override the real severity here.
    incidents = [_incident(IncidentStatus.FAILED.value, severity="HEALTH_WARN")]
    assert compute_cluster_status(incidents, heartbeat_stale=False) == "WARN"


def test_incident_with_no_recorded_severity_means_warn_not_err():
    # Missing severity (legacy row predating this column, or any future code
    # path that forgets to set it) must default to the conservative WARN,
    # never a false ERR backed by no real evidence.
    incidents = [_incident(IncidentStatus.FAILED.value, severity=None)]
    assert compute_cluster_status(incidents, heartbeat_stale=False) == "WARN"


def test_no_incidents_at_all_means_ok_when_heartbeat_is_fresh():
    assert compute_cluster_status([], heartbeat_stale=False) == "OK"


def test_no_incidents_and_stale_heartbeat_means_unknown():
    # The bug this guards against: 0 incidents can mean "cluster is healthy"
    # OR "Watcher has never successfully reached the cluster" — those are
    # indistinguishable from `incidents` alone. Without a stale heartbeat,
    # "OK" would be a reassuring green status backed by zero evidence.
    assert compute_cluster_status([], heartbeat_stale=True) == "UNKNOWN"


def test_failed_chat_request_incident_does_not_count_as_cluster_err():
    # 2026-07-23 regression: confirm_chat_action's synthetic CHAT_REQUEST
    # incident is bookkeeping for a chat-confirmed action, not real Ceph
    # health — a failed chat action (e.g. create_pool erroring out) must
    # never flip the whole cluster status badge to ERR when `ceph health`
    # itself never left OK.
    incidents = [_incident(IncidentStatus.FAILED.value, ceph_code=CHAT_REQUEST_CEPH_CODE)]
    assert compute_cluster_status(incidents, heartbeat_stale=False) == "OK"


def test_failed_chat_request_incident_does_not_mask_a_real_warn():
    incidents = [
        _incident(IncidentStatus.FAILED.value, ceph_code=CHAT_REQUEST_CEPH_CODE),
        _incident(IncidentStatus.NEW.value),
    ]
    assert compute_cluster_status(incidents, heartbeat_stale=False) == "WARN"


def test_open_incident_status_ignores_heartbeat_staleness():
    # Real recorded incidents are historical fact regardless of whether the
    # connection is currently stale — only the "nothing recorded yet" case
    # is ambiguous enough to need the heartbeat signal.
    incidents = [_incident(IncidentStatus.NEW.value)]
    assert compute_cluster_status(incidents, heartbeat_stale=True) == "WARN"


def _heartbeat(success: bool, polled_at: datetime, mon_node: str | None = "10.20.1.150") -> WatcherHeartbeat:
    return WatcherHeartbeat(
        id=1, success=success, mon_node=mon_node, error_message=None, polled_at=polled_at
    )


def test_heartbeat_is_stale_when_none_ever_recorded():
    assert is_heartbeat_stale(None) is True


def test_heartbeat_is_stale_when_last_poll_failed():
    hb = _heartbeat(success=False, polled_at=datetime.utcnow())
    assert is_heartbeat_stale(hb) is True


def test_heartbeat_is_stale_when_too_old(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "watcher_poll_interval_seconds", 15)
    old_poll = datetime.utcnow() - timedelta(seconds=15 * 3 + 1)
    hb = _heartbeat(success=True, polled_at=old_poll)

    assert is_heartbeat_stale(hb) is True


def test_heartbeat_is_not_stale_when_recent_and_successful(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "watcher_poll_interval_seconds", 15)
    recent_poll = datetime.utcnow() - timedelta(seconds=5)
    hb = _heartbeat(success=True, polled_at=recent_poll)

    assert is_heartbeat_stale(hb) is False

"""Shared Alert Center lifecycle checks used by Dashboard and Watcher."""

from datetime import datetime

from shared.models import Incident


def is_active_mute(incident: Incident, *, now: datetime | None = None) -> bool:
    now = now or datetime.utcnow()
    return bool(incident.muted_until and incident.muted_until > now)


def inherit_active_mute(session, incident: Incident, *, now: datetime | None = None) -> bool:
    """Carry a still-active same-code mute onto a newly-created Incident.

    Alert Center controls must not stop detection or Incident creation. This
    only carries the notification preference forward for the mute window.
    """
    now = now or datetime.utcnow()
    if is_active_mute(incident, now=now):
        return True
    source = (
        session.query(Incident)
        .filter(Incident.id != incident.id)
        .filter(Incident.cluster_id == incident.cluster_id)
        .filter(Incident.ceph_code == incident.ceph_code)
        .filter(Incident.muted_until > now)
        .order_by(Incident.muted_until.desc(), Incident.updated_at.desc())
        .first()
    )
    if source is None:
        return False
    incident.muted_until = source.muted_until
    incident.muted_by = source.muted_by
    return True

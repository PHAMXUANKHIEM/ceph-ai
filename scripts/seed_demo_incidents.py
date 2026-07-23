"""Seed 1-2 demo Incident rows so the Dashboard has something to show.

Not part of epics.md's original Story 1.5 AC — added because Story 1.3/1.4
(the Watcher that would create real Incidents) were deliberately skipped for
this reprioritized pass. Safe to run multiple times (inserts new rows each
time; does not deduplicate).

Usage (from ceph-aiops/, with the venv active):
    python -m scripts.seed_demo_incidents
"""

from datetime import datetime, timedelta

from shared.db import Base, SessionLocal, engine
from shared.models import Incident, IncidentStatus


def seed() -> None:
    Base.metadata.create_all(engine)
    now = datetime.utcnow()
    demo_incidents = [
        Incident(
            ceph_code="OSD_DOWN",
            status=IncidentStatus.NEW.value,
            log_excerpt="osd.3 marked down after no beacon for 20s on rnd-khiempx-lab-ceph2",
            detected_at=now - timedelta(minutes=3),
        ),
        Incident(
            ceph_code="MON_CLOCK_SKEW",
            status=IncidentStatus.RESOLVED.value,
            log_excerpt="clock skew detected on rnd-khiempx-lab-ceph1, resynced via chrony",
            detected_at=now - timedelta(hours=2),
        ),
    ]
    with SessionLocal() as session:
        session.add_all(demo_incidents)
        session.commit()
    print(f"Seeded {len(demo_incidents)} demo Incident(s).")


if __name__ == "__main__":
    seed()

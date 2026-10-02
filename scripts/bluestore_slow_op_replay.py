"""Replay historical BLUESTORE_SLOW_OP_ALERT Incidents through the WP1.2 gate.

Input: a JSON list of past Incidents, each ``{"start", "end", "osds":
[[osd_id, host], ...]}`` (ISO timestamps; ``osds`` parsed from the
"--- <host> (osd.N) ---" headers of the log excerpt). While any Incident
covers a poll, the check is taken as present with the union of their OSDs.
Polls every sample interval, applies watcher/bluestore_slow_ops.py's own
decision rules, and reports how many Incidents the gate would have opened
against how many were opened, plus whether each of the busiest days (the
"waves") still produced at least one Incident.

Read-only: no database or SSH access.

Usage: python scripts/bluestore_slow_op_replay.py history.json [--waves 3]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.settings import settings  # noqa: E402
from watcher.bluestore_slow_ops import _EPISODE_GAP_INTERVALS, _reasons, percentile_95  # noqa: E402


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=None)


def replay(incidents: list[dict], waves: int = 3) -> dict:
    step = timedelta(seconds=settings.bluestore_slow_op_sample_interval_seconds)
    gap = step * _EPISODE_GAP_INTERVALS
    spans = sorted((_parse(i["start"]), _parse(i["end"]), i.get("osds") or []) for i in incidents)
    if not spans:
        return {"incidents_before": 0, "incidents_after": 0}
    now, finish = spans[0][0], max(end for _start, end, _osds in spans)
    samples: list[tuple[datetime, int]] = []
    opened: list[dict] = []
    episode_start: datetime | None = None
    last_seen: datetime | None = None
    incident_open = False
    while now <= finish:
        covering = [osds for start, end, osds in spans if start <= now <= end]
        if covering:
            hosts = {int(osd): host for osds in covering for osd, host in osds}
            osd_ids = tuple(sorted(hosts)) or (-1,)
            if last_seen is None or now - last_seen > gap:
                episode_start, incident_open = now, False
            last_seen = now
            assert episode_start is not None
            baseline = [
                count for at, count in samples
                if now - timedelta(days=settings.bluestore_slow_op_baseline_days) <= at < episode_start
            ]
            p95 = percentile_95(baseline) if len(baseline) >= settings.bluestore_slow_op_min_baseline_samples else None
            reasons = _reasons(osd_ids, hosts, int((now - episode_start).total_seconds()), p95)
            samples.append((now, len(osd_ids)))
            if reasons and not incident_open:
                incident_open = True
                opened.append({"at": now.isoformat(), "reasons": list(reasons), "osd_ids": list(osd_ids)})
        elif last_seen is not None and now - last_seen > gap:
            incident_open = False
        now += step
    busiest = [day for day, _count in Counter(s.isoformat()[:10] for s, _e, _o in spans).most_common(waves)]
    opened_days = {item["at"][:10] for item in opened}
    before = len(incidents)
    return {
        "incidents_before": before,
        "incidents_after": len(opened),
        "reduction_percent": round(100 * (1 - len(opened) / before), 1) if before else None,
        "open_reasons": dict(Counter(reason for item in opened for reason in item["reasons"])),
        "waves": {day: day in opened_days for day in sorted(busiest)},
        "opened": opened,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("history", type=Path)
    parser.add_argument("--waves", type=int, default=3)
    args = parser.parse_args(argv)
    result = replay(json.loads(args.history.read_text(encoding="utf-8")), waves=args.waves)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

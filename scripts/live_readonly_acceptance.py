#!/usr/bin/env python3
"""Live read-only acceptance against every active Ceph cluster.

This is the plan's 5.3 wave: health, inventory, pool/PG/CRUSH, RBD and RGW
are queried on real clusters without any mutation, and the evidence records
per-command exit status, latency, SSH call count and the Ceph version matrix.

Safety:

* every probe is a fixed inner command matched against ``READ_ONLY_COMMANDS``;
* every SSH command that reaches the transport (including the enrichment
  queries issued by the client helpers) is checked against
  ``MUTATING_PATTERN`` and refused before it is sent;
* ``--window HH:MM-HH:MM`` (UTC) refuses to run outside the safety window.

The report is written to its own directory so it cannot be mistaken for
fixture evidence.  Exit status: 0 passed, 1 a probe failed or was refused,
2 the cluster/version matrix is smaller than required.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCHEMA = "ceph-ai.live-readonly-acceptance.v1"
MISSING_POOL = "ceph-ai-acceptance-missing-pool"
MAX_RBD_POOLS = 2

READ_ONLY_COMMANDS = (
    re.compile(r"^ceph (health detail|status|versions|df|pg stat|osd tree|osd pool ls detail|osd crush rule dump)$"),
    re.compile(r"^rbd du --pool [A-Za-z0-9._-]+$"),
    re.compile(r"^radosgw-admin bucket list$"),
)
MUTATING_VERBS = frozenset({
    "create", "rm", "remove", "delete", "purge", "set", "unset", "reweight", "out", "in", "mv",
    "rename", "resize", "flatten", "rollback", "enable", "disable", "restart", "stop", "start",
    "kill", "mark", "repair", "scrub", "deep-scrub", "import", "export", "map", "unmap", "lock",
    "evict", "trash", "protect", "unprotect", "add", "clear", "reset", "apply", "upgrade",
})
# A Ceph CLI invocation inside the (possibly wrapped or batched) SSH command,
# up to the next shell separator or quote.
CLI_SEGMENT = re.compile(r"""(?:^|[\s;&|'"(])((?:ceph|rbd|radosgw-admin)\s[^;&|\n'"()]*)""")
SUBCOMMAND_WORDS = 4


class RefusedCommand(RuntimeError):
    """A command that is not provably read-only was about to be sent."""


def assert_read_only_probe(inner_command: str) -> None:
    if not any(pattern.match(inner_command) for pattern in READ_ONLY_COMMANDS):
        raise RefusedCommand(f"probe is not on the read-only allowlist: {inner_command}")


CLI_BINARIES = frozenset({"ceph", "rbd", "radosgw-admin"})


def cli_segments(remote_command: str) -> list[str]:
    segments = []
    for match in CLI_SEGMENT.finditer(remote_command):
        words = match.group(1).split()
        # `docker exec ceph ceph ...`: a container named like the binary must
        # not consume one of the subcommand words that are checked below.
        while len(words) > 1 and words[0] in CLI_BINARIES and words[1] in CLI_BINARIES:
            words.pop(0)
        segments.append(" ".join(words))
    return segments


def assert_read_only_transport(remote_command: str) -> None:
    """Refuse unless every Ceph CLI call in the command is read-only.

    Wrappers (sudo/docker exec/cephadm shell/timeout/flock) and the batch
    runner's bash scaffolding are not Ceph calls; a command that contains no
    recognisable Ceph call at all is refused rather than trusted.
    """
    segments = cli_segments(remote_command)
    if not segments:
        raise RefusedCommand(f"no Ceph CLI call found in transport command: {remote_command[:120]}")
    for segment in segments:
        positional = [word for word in segment.split()[1:] if not word.startswith("-")]
        verbs = set(positional[:SUBCOMMAND_WORDS]) & MUTATING_VERBS
        if verbs:
            raise RefusedCommand(f"refused mutating Ceph call ({', '.join(sorted(verbs))}): {segment}")


def within_window(window: str | None, now: datetime) -> bool:
    if not window:
        return True
    start_text, _, end_text = window.partition("-")
    start = dtime.fromisoformat(start_text)
    end = dtime.fromisoformat(end_text)
    current = now.astimezone(timezone.utc).time().replace(tzinfo=None)
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


def p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)], 2)


@dataclass
class TransportMeter:
    """Counts, times and guards every SSH command sent by the Ceph client."""

    calls: list[dict[str, Any]] = field(default_factory=list)

    def wrap(self, original: Callable[..., str]) -> Callable[..., str]:
        def guarded(host: str, command: str, *args: Any, **kwargs: Any) -> str:
            assert_read_only_transport(command)
            started = time.monotonic()
            record: dict[str, Any] = {"host": host, "commands": [item[:160] for item in cli_segments(command)]}
            try:
                return original(host, command, *args, **kwargs)
            except Exception as exc:
                record["error"] = type(exc).__name__
                raise
            finally:
                record["duration_ms"] = round((time.monotonic() - started) * 1000, 2)
                self.calls.append(record)

        return guarded


def _redact(value: str) -> str:
    try:
        from shared.ai_redaction import redact_text
    except Exception:  # pragma: no cover - redaction must not hide the result
        return value[:500]
    return redact_text(value)[:500]


def run_probe(name: str, inner_command: str, runner: Callable[[str], Any]) -> tuple[dict[str, Any], Any]:
    assert_read_only_probe(inner_command)
    started = time.monotonic()
    result: dict[str, Any] = {"probe": name, "command": inner_command}
    payload: Any = None
    try:
        host, payload = runner(inner_command)
        result.update(status="ok", exit_code=0, mon_host=host)
    except RefusedCommand as exc:
        result.update(status="refused", exit_code=None, error=str(exc))
    except Exception as exc:
        result.update(status="error", exit_code=getattr(exc, "exit_status", 1), error=_redact(str(exc)))
    result["duration_ms"] = round((time.monotonic() - started) * 1000, 2)
    return result, payload


def rbd_pools(pool_detail: Any) -> list[str]:
    pools = pool_detail if isinstance(pool_detail, list) else []
    names = [
        str(pool.get("pool_name"))
        for pool in pools
        if isinstance(pool, dict) and "rbd" in (pool.get("application_metadata") or {})
    ]
    return sorted(name for name in names if name)[:MAX_RBD_POOLS]


def has_rgw(status: Any) -> bool:
    services = ((status or {}).get("servicemap") or {}).get("services") if isinstance(status, dict) else None
    return isinstance(services, dict) and "rgw" in services


def ceph_release(versions: Any) -> list[str]:
    overall = (versions or {}).get("overall") if isinstance(versions, dict) else None
    if not isinstance(overall, dict):
        return []
    releases = set()
    for label in overall:
        match = re.search(r"ceph version (\d+)\.", label)
        if match:
            releases.add(match.group(1))
    return sorted(releases)


def missing_pool_probe(query_inventory: Callable[[str], Any]) -> dict[str, Any]:
    """An unreadable pool must surface as an error, never as 0 images."""
    started = time.monotonic()
    outcome: dict[str, Any]
    try:
        rows = query_inventory(MISSING_POOL)
    except RefusedCommand as exc:
        return {"probe": "error_not_zero", "status": "refused", "error": str(exc)}
    except Exception as exc:
        outcome = {"probe": "error_not_zero", "status": "ok", "observed": "error", "error": _redact(str(exc))}
    else:
        outcome = {
            "probe": "error_not_zero",
            "status": "failed",
            "observed": f"returned {type(rows).__name__} of length {len(rows) if hasattr(rows, '__len__') else '?'}",
            "error": "a missing pool was reported as data instead of an error",
        }
    outcome["duration_ms"] = round((time.monotonic() - started) * 1000, 2)
    return outcome


def probe_cluster(name: str, connection: tuple, client: Any) -> dict[str, Any]:
    def runner(inner_command: str) -> Any:
        return client.run_ceph_json_command_with(*connection, inner_command)

    probes: list[dict[str, Any]] = []
    payloads: dict[str, Any] = {}
    for probe, command in (
        ("health", "ceph health detail"),
        ("status", "ceph status"),
        ("versions", "ceph versions"),
        ("inventory", "ceph osd tree"),
        ("pools", "ceph osd pool ls detail"),
        ("capacity", "ceph df"),
        ("pg", "ceph pg stat"),
        ("crush", "ceph osd crush rule dump"),
    ):
        result, payloads[probe] = run_probe(probe, command, runner)
        probes.append(result)

    for pool in rbd_pools(payloads.get("pools")):
        result, _ = run_probe(f"rbd:{pool}", f"rbd du --pool {pool}", runner)
        probes.append(result)
    if has_rgw(payloads.get("status")):
        result, _ = run_probe("rgw", "radosgw-admin bucket list", runner)
        probes.append(result)
    else:
        probes.append({"probe": "rgw", "status": "not_applicable", "reason": "no rgw in servicemap"})

    probes.append(missing_pool_probe(lambda pool: client.query_rbd_inventory_with(pool, *connection)))
    return {
        "cluster": name,
        "exec_mode": connection[4],
        "mon_nodes": len(connection[0]),
        "ceph_releases": ceph_release(payloads.get("versions")),
        "health_status": (payloads.get("health") or {}).get("status") if isinstance(payloads.get("health"), dict) else None,
        "probes": probes,
    }


def probe_dashboard(base_url: str, cookie: str, endpoints: list[str], rounds: int) -> dict[str, Any]:
    results = []
    for endpoint in endpoints:
        durations: list[float] = []
        statuses: list[int] = []
        for _ in range(rounds):
            request = urllib.request.Request(base_url.rstrip("/") + endpoint, headers={"Cookie": cookie})
            started = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310 - operator-supplied URL
                    statuses.append(response.status)
            except Exception as exc:
                statuses.append(getattr(exc, "code", 0) or 0)
            durations.append((time.monotonic() - started) * 1000)
        results.append({
            "endpoint": endpoint,
            "p95_ms": p95(durations),
            "ok": all(200 <= status < 300 for status in statuses),
            "statuses": sorted(set(statuses)),
        })
    return {"endpoints": results, "ok": all(item["ok"] for item in results)}


def summarise(report: dict[str, Any], *, min_clusters: int, min_releases: int) -> tuple[str, int]:
    probes = [probe for cluster in report["clusters"] for probe in cluster["probes"]]
    failed = [probe for probe in probes if probe["status"] in {"error", "failed", "refused"}]
    dashboard = report.get("dashboard")
    if failed or (dashboard and not dashboard["ok"]):
        return "failed", 1
    releases = {release for cluster in report["clusters"] for release in cluster["ceph_releases"]}
    if len(report["clusters"]) < min_clusters or len(releases) < min_releases:
        return "incomplete_matrix", 2
    return "passed", 0


def load_clusters(selected: list[str]) -> list[tuple[str, tuple]]:
    from dashboard.cluster_scope import cluster_connection
    from shared import db
    from shared.clusters import list_active_clusters

    with db.SessionLocal() as session:
        clusters = list_active_clusters(session)
        chosen = [
            (cluster.name, cluster_connection(cluster))
            for cluster in clusters
            if not selected or cluster.name in selected or cluster.id in selected
        ]
    return chosen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cluster", action="append", default=[], help="cluster name or id (default: all active)")
    parser.add_argument("--min-clusters", type=int, default=2)
    parser.add_argument("--min-releases", type=int, default=2)
    parser.add_argument("--window", help="UTC safety window HH:MM-HH:MM; refuse to run outside it")
    parser.add_argument("--dashboard-url", help="optional base URL for authenticated API latency probes")
    parser.add_argument("--dashboard-cookie-env", default="CEPH_AI_ACCEPTANCE_COOKIE")
    parser.add_argument("--endpoint", action="append", default=[])
    parser.add_argument("--rounds", type=int, default=20)
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    if not within_window(args.window, now):
        print(f"REFUSED: {now:%H:%M}Z is outside the safety window {args.window}")
        return 1

    import os

    from watcher import ceph_client

    meter = TransportMeter()
    ceph_client._run_remote_command_with = meter.wrap(ceph_client._run_remote_command_with)

    report: dict[str, Any] = {
        "schema": SCHEMA,
        "evidence_kind": "live",
        "started_at": now.isoformat(),
        "window": args.window,
        "command_timeouts_seconds": {
            "default": getattr(ceph_client, "MCP_COMMAND_TIMEOUT_SECONDS", None),
            "cephadm": getattr(ceph_client, "CEPHADM_COMMAND_TIMEOUT_SECONDS", None),
        },
        "clusters": [],
    }
    for name, connection in load_clusters(args.cluster):
        report["clusters"].append(probe_cluster(name, connection, ceph_client))

    if args.dashboard_url:
        cookie = os.environ.get(args.dashboard_cookie_env, "")
        endpoints = args.endpoint or ["/api/system/health", "/api/system/reliability", "/api/dashboard/health"]
        report["dashboard"] = probe_dashboard(args.dashboard_url, cookie, endpoints, args.rounds)

    durations = [call["duration_ms"] for call in meter.calls]
    report["ssh"] = {
        "calls": len(meter.calls),
        "errors": sum(1 for call in meter.calls if "error" in call),
        "p95_ms": p95(durations),
        "max_ms": max(durations) if durations else None,
    }
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["status"], exit_code = summarise(report, min_clusters=args.min_clusters, min_releases=args.min_releases)

    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "live-readonly-report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (args.output / "live-readonly-ssh-calls.json").write_text(
        json.dumps(meter.calls, indent=1) + "\n", encoding="utf-8"
    )
    releases = sorted({r for cluster in report["clusters"] for r in cluster["ceph_releases"]})
    print(
        f"LIVE READ-ONLY {report['status'].upper()}: clusters={len(report['clusters'])} "
        f"releases={releases} ssh_calls={report['ssh']['calls']} ssh_p95_ms={report['ssh']['p95_ms']}"
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

"""Deterministic triage from collected evidence (autonomy plan WP3.4).

Before an LLM is asked, simple rules read the read-only evidence of
WP3.3 (``incident_evidence`` rows) for the most frequent fault families
and draw the conclusion an operator would.  Every conclusion cites the
evidence it used; when the evidence is missing or ambiguous the result is
``UNKNOWN`` and the LLM (or a human) decides.  Rules never propose a
mutating action on their own: at most they name an approval-gated
``action_id`` that already exists in worker/policy/action_policy.yaml.
"""

from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable

from shared.autonomy_kpi import fault_family

UNKNOWN = "UNKNOWN"
_PING_RECEIVED = re.compile(r"(\d+)\s+(?:packets\s+)?received")
_UPTIME = re.compile(r"\bup\s+(?:(\d+)\s+days?,\s*)?(?:(\d+):(\d+)|(\d+)\s+min)")
RECENT_BOOT_MINUTES = 15
LATENCY_OUTLIER_FACTOR = 3.0
LATENCY_HIGH_MS = 100


@dataclass(frozen=True)
class Evidence:
    collector_id: str
    status: str
    output: str
    ref: str = ""        # incident_evidence.id, cited by the conclusion


@dataclass
class Triage:
    family: str
    conclusion: str
    summary: str = ""
    recommendation: str = ""
    action_id: str | None = None          # approval-gated action, never executed here
    confidence: float = 0.0
    cited: list[str] = field(default_factory=list)

    @property
    def is_known(self) -> bool:
        return self.conclusion != UNKNOWN

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _normalise(rows: Iterable[Any]) -> dict[str, Evidence]:
    evidence: dict[str, Evidence] = {}
    for row in rows:
        get = row.get if isinstance(row, dict) else lambda key, row=row: getattr(row, key, None)
        item = Evidence(str(get("collector_id") or ""), str(get("status") or ""),
                        str(get("output_redacted") or get("output") or ""), str(get("id") or ""))
        evidence[item.collector_id] = item
    return evidence


def _json(item: Evidence | None) -> Any:
    if item is None or item.status != "ok":
        return None
    try:
        return json.loads(item.output)
    except ValueError:
        return None          # truncated or not JSON: treated as unavailable


def _cite(*items: Evidence | None) -> list[str]:
    return [item.ref or item.collector_id for item in items if item is not None]


def ping_ok(item: Evidence | None) -> bool | None:
    if item is None or item.status not in {"ok", "error"}:
        return None
    match = _PING_RECEIVED.search(item.output)
    if match:
        return int(match.group(1)) > 0
    return False if item.status == "error" else None


def uptime_minutes(text: str) -> int | None:
    match = _UPTIME.search(text or "")
    if not match:
        return None
    days, hours, minutes, only_minutes = match.groups()
    total = int(days or 0) * 1440
    if only_minutes is not None:
        return total + int(only_minutes)
    return total + int(hours) * 60 + int(minutes)


def osd_states(tree: Any) -> dict[int, str]:
    nodes = tree.get("nodes", []) if isinstance(tree, dict) else []
    return {int(node["id"]): str(node.get("status", "")) for node in nodes
            if isinstance(node, dict) and node.get("type") == "osd" and "id" in node}


def _node_unreachable(evidence: dict[str, Evidence]) -> Triage:
    family = "NODE_UNREACHABLE"
    ping, ssh, tree = evidence.get("mon_ping"), evidence.get("host_uptime"), evidence.get("ceph_osd_tree")
    reachable = ping_ok(ping)
    ssh_ok = None if ssh is None else ssh.status == "ok"
    states = osd_states(_json(tree))
    down = sorted(osd for osd, state in states.items() if state != "up")
    if ssh is not None and ssh_ok:
        minutes = uptime_minutes(ssh.output)
        if minutes is not None and minutes <= RECENT_BOOT_MINUTES:
            return Triage(family, "HOST_RECENTLY_REBOOTED", f"Host vừa khởi động lại ({minutes} phút trước).",
                          "Theo dõi; không cần hành động.", confidence=0.9, cited=_cite(ssh))
        return Triage(family, "TRANSIENT", "Host đã trả lời SSH trở lại khi thu bằng chứng.",
                      "Theo dõi; nếu lặp lại nhiều lần xem cảnh báo host chập chờn.", confidence=0.8, cited=_cite(ssh, ping))
    if reachable and ssh_ok is False:
        return Triage(family, "SSH_UNAVAILABLE", "MON ping được host nhưng SSH thất bại.",
                      "Kiểm tra sshd/firewall trên host; không reboot.", confidence=0.85, cited=_cite(ping, ssh))
    if reachable is False and states:
        if down:
            return Triage(family, "HOST_DOWN", f"Không ping được host; có OSD down: {down[:6]}.",
                          "Kiểm tra nguồn/IPMI/console của host.", confidence=0.7, cited=_cite(ping, tree))
        return Triage(family, "MGMT_NETWORK", "Không ping được host nhưng mọi OSD vẫn up: data plane ổn.",
                      "Kiểm tra mạng quản trị; không reboot host.", confidence=0.85, cited=_cite(ping, tree))
    return Triage(family, UNKNOWN, "Bằng chứng chưa đủ để kết luận.", cited=_cite(ping, ssh, tree))


def _perf_rows(perf: Any) -> dict[int, float]:
    if isinstance(perf, dict):
        perf = (perf.get("osdstats") or perf).get("osd_perf_infos", [])
    latencies = {}
    for row in perf if isinstance(perf, list) else []:
        stats = row.get("perf_stats", {}) if isinstance(row, dict) else {}
        if "id" in row and "commit_latency_ms" in stats:
            latencies[int(row["id"])] = float(stats["commit_latency_ms"])
    return latencies


def _osd_latency(evidence: dict[str, Evidence], osd_id: int | None) -> Triage:
    family = "OSD_LATENCY_HIGH"
    perf = evidence.get("ceph_osd_perf")
    latencies = _perf_rows(_json(perf))
    if osd_id is None or osd_id not in latencies or len(latencies) < 3:
        return Triage(family, UNKNOWN, "Thiếu số liệu latency để so sánh.", cited=_cite(perf))
    target = latencies[osd_id]
    others = [value for osd, value in latencies.items() if osd != osd_id]
    baseline = statistics.median(others)
    high_share = sum(1 for value in latencies.values() if value >= LATENCY_HIGH_MS) / len(latencies)
    if high_share >= 0.5:
        return Triage(family, "CLUSTER_WIDE_LOAD",
                      f"{high_share:.0%} OSD có commit latency ≥ {LATENCY_HIGH_MS} ms: tải chung, không riêng osd.{osd_id}.",
                      "Xem recovery/backfill và client I/O; không restart OSD.", confidence=0.75, cited=_cite(perf))
    if target >= LATENCY_OUTLIER_FACTOR * max(baseline, 1.0):
        return Triage(family, "SINGLE_OSD_OUTLIER",
                      f"osd.{osd_id} commit {target:.0f} ms, trung vị các OSD khác {baseline:.0f} ms.",
                      "Nghi đĩa của OSD này: xem device health/SMART trước khi restart.",
                      confidence=0.75, cited=_cite(perf, evidence.get("ceph_osd_slow_ops")))
    return Triage(family, "RECOVERED", f"osd.{osd_id} hiện {target:.0f} ms, gần mức chung ({baseline:.0f} ms).",
                  "Theo dõi; không hành động.", confidence=0.6, cited=_cite(perf))


def _clock_skew(evidence: dict[str, Evidence]) -> Triage:
    family = "MON_CLOCK_SKEW"
    sync = evidence.get("ceph_time_sync")
    payload = _json(sync)
    status = payload.get("time_skew_status") if isinstance(payload, dict) else None
    if not isinstance(status, dict) or not status:
        return Triage(family, UNKNOWN, "Không đọc được time-sync-status.", cited=_cite(sync))
    skewed = sorted(mon for mon, info in status.items() if isinstance(info, dict) and info.get("health") != "HEALTH_OK")
    if not skewed:
        return Triage(family, "RECOVERED", "Mọi MON đã đồng bộ giờ.", "Theo dõi; không hành động.",
                      confidence=0.8, cited=_cite(sync))
    return Triage(family, "MON_SKEWED", f"MON lệch giờ: {', '.join(skewed)}.",
                  "Đồng bộ NTP trên MON đó (cần duyệt).", action_id="resync_ntp", confidence=0.8, cited=_cite(sync))


def triage(ceph_code: str | None, rows: Iterable[Any]) -> Triage:
    family = fault_family(ceph_code)
    evidence = _normalise(rows)
    suffix = str(ceph_code or "").partition(":")[2]
    if family == "NODE_UNREACHABLE":
        return _node_unreachable(evidence)
    if family == "OSD_LATENCY_HIGH":
        match = re.fullmatch(r"(?:osd\.)?(\d{1,6})", suffix)
        return _osd_latency(evidence, int(match.group(1)) if match else None)
    if family == "MON_CLOCK_SKEW":
        return _clock_skew(evidence)
    return Triage(family, UNKNOWN, "Chưa có luật cho fault family này.")

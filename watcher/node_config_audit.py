"""Check each Ceph node's OS settings against what keeps a small node alive.

09/10/2026: ceph2 was reset after kswapd0/khugepaged soft-locked during
memory reclaim. All three CS-LAB VMs ran MGLRU, transparent hugepages
"always", no swap, a 66 MB min_free_kbytes and an hourly dnf makecache on
8 GB of RAM, with a volatile journal and one machine-id shared by all three.
Finding each of those took a manual read on every node.

Every ``node_config_audit_interval_seconds`` the Watcher runs one read-only
SSH command per node, evaluates the facts with the rules below and publishes
the findings as the ``node_config`` snapshot section. Telegram hears about it
only when the set of findings changes.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

SECTION = "node_config"
FACTS_TIMEOUT_SECONDS = 20
# What a MON/MGR/RGW and the OS need besides the OSDs on an 8 GB node.
_NON_OSD_RESERVE_BYTES = int(1.5 * 1024**3)
_MIN_FREE_FLOOR_KB = 131072
_MEMORY_BUDGET_RATIO = 0.85

FACTS_COMMAND = " ; ".join((
    'echo "HOST=$(hostname -s)"',
    'echo "MACHINE_ID=$(cat /etc/machine-id 2>/dev/null)"',
    "awk '/^MemTotal:/{print \"MEM_KB=\"$2} /^SwapTotal:/{print \"SWAP_KB=\"$2}' /proc/meminfo",
    'echo "LRU_GEN=$(cat /sys/kernel/mm/lru_gen/enabled 2>/dev/null || echo none)"',
    "echo \"THP=$(grep -o '\\[[a-z]*\\]' /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null | tr -d '[]')\"",
    'echo "MIN_FREE_KB=$(cat /proc/sys/vm/min_free_kbytes)"',
    'echo "JOURNAL=$([ -d /var/log/journal ] && echo persistent || echo volatile)"',
    'echo "MAKECACHE=$(systemctl is-enabled dnf-makecache.timer 2>/dev/null || echo none)"',
    'echo "OSDS=$(pgrep -c -x ceph-osd || true)"',
    'echo "NTP_SYNC=$(timedatectl show -p NTPSynchronized --value 2>/dev/null || echo unknown)"',
    'echo "EPOCH=$(date -u +%s.%N)"',
))
# 09/10/2026: the Ceph AI host ran 37.7 s behind three NTP-synced nodes with
# no NTP service at all; snapshot ages, incident times and forecast/outcome
# matching against cluster logs were all off by that much.
CLOCK_SKEW_LIMIT_SECONDS = 5.0


@dataclass(frozen=True)
class Finding:
    host: str
    code: str
    severity: str  # "warning" | "info"
    message: str
    fix: str

    def as_dict(self) -> dict:
        return {"host": self.host, "code": self.code, "severity": self.severity,
                "message": self.message, "fix": self.fix}


def parse_facts(output: str) -> dict[str, str]:
    facts: dict[str, str] = {}
    for line in output.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.isupper():
            facts[key] = value.strip()
    return facts


def _int(facts: dict[str, str], key: str) -> int | None:
    value = facts.get(key, "")
    return int(value) if value.isdigit() else None


def _host_findings(host: str, facts: dict[str, str], osd_memory_target: int | None) -> list[Finding]:
    found: list[Finding] = []
    mem_kb, swap_kb, min_free = _int(facts, "MEM_KB"), _int(facts, "SWAP_KB"), _int(facts, "MIN_FREE_KB")
    lru_on = facts.get("LRU_GEN", "none") not in ("none", "0x0000", "n", "")
    if lru_on and swap_kb == 0:
        found.append(Finding(host, "RECLAIM_LOCKUP_RISK", "warning",
                             "MGLRU đang bật và không có swap: kswapd có thể kẹt khi thu hồi bộ nhớ (ceph2, 09/10).",
                             "echo n > /sys/kernel/mm/lru_gen/enabled (giữ qua reboot bằng tmpfiles.d) hoặc thêm swap nhỏ"))
    if facts.get("THP") == "always":
        found.append(Finding(host, "THP_ALWAYS", "warning",
                             "transparent_hugepage=always: khugepaged gom trang lớn cho mọi tiến trình.",
                             "đặt madvise (khuyến nghị cho Ceph/RocksDB)"))
    if min_free is not None and min_free < _MIN_FREE_FLOOR_KB and (mem_kb or 0) >= 4 * 1024 * 1024:
        found.append(Finding(host, "MIN_FREE_LOW", "warning",
                             f"vm.min_free_kbytes={min_free} ({min_free // 1024} MB): ít chỗ trống khi thu hồi bộ nhớ.",
                             "vm.min_free_kbytes=262144 trong /etc/sysctl.d"))
    if facts.get("JOURNAL") == "volatile":
        found.append(Finding(host, "JOURNAL_VOLATILE", "warning",
                             "journal không lưu xuống đĩa: log kernel của lần treo/reset bị mất.",
                             "mkdir /var/log/journal; systemctl restart systemd-journald"))
    if facts.get("NTP_SYNC") == "no":
        found.append(Finding(host, "NTP_UNSYNCED", "warning",
                             "Đồng hồ node không được đồng bộ NTP: log, sự cố và MON quorum lệch giờ.",
                             "systemctl enable --now chronyd; chronyc makestep"))
    if facts.get("MAKECACHE") == "enabled":
        found.append(Finding(host, "DNF_MAKECACHE_ENABLED", "info",
                             "dnf-makecache.timer bật: làm mới metadata gói định kỳ, tốn RAM đột ngột.",
                             "systemctl disable --now dnf-makecache.timer"))
    osds = _int(facts, "OSDS")
    if osd_memory_target and osds and mem_kb:
        need = osds * osd_memory_target + _NON_OSD_RESERVE_BYTES
        have = mem_kb * 1024
        if need > have * _MEMORY_BUDGET_RATIO:
            found.append(Finding(host, "MEMORY_OVERCOMMIT", "warning",
                                 f"{osds} OSD x osd_memory_target {osd_memory_target // 1024**2} MB + các daemon khác "
                                 f"~{need / 1024**3:.1f} GB trên {have / 1024**3:.1f} GB RAM.",
                                 "giảm osd_memory_target, bớt daemon trên node hoặc thêm RAM"))
    return found


def evaluate(facts_by_host: dict[str, dict[str, str]], osd_memory_target: int | None) -> list[Finding]:
    """Findings for every node; pure, so it is testable offline.

    ``facts_by_host`` may list one VM under several addresses; the hostname
    reported by the node itself deduplicates them.
    """
    by_name: dict[str, tuple[str, dict[str, str]]] = {}
    for address, facts in facts_by_host.items():
        by_name.setdefault(facts.get("HOST") or address, (address, facts))
    findings: list[Finding] = []
    for name, (_address, facts) in sorted(by_name.items()):
        findings.extend(_host_findings(name, facts, osd_memory_target))
    owners: dict[str, list[str]] = {}
    for name, (_address, facts) in by_name.items():
        if facts.get("MACHINE_ID"):
            owners.setdefault(facts["MACHINE_ID"], []).append(name)
    findings.extend(_ceph_ai_clock(by_name))
    for machine_id, names in owners.items():
        if len(names) > 1:
            findings.append(Finding(", ".join(sorted(names)), "DUPLICATE_MACHINE_ID", "warning",
                                    f"{len(names)} node dùng chung machine-id {machine_id[:8]}…: journal/monitoring dễ lẫn.",
                                    "rm /etc/machine-id; systemd-machine-id-setup (từng node, ngoài giờ)"))
    return findings


def _ceph_ai_clock(by_name: dict[str, tuple[str, dict[str, str]]]) -> list[Finding]:
    """When every NTP-synced node disagrees with this host by the same amount, this host is off."""
    offsets = []
    for _address, facts in by_name.values():
        try:
            offsets.append(float(facts["EPOCH"]) - float(facts["_LOCAL_EPOCH"]))
        except (KeyError, ValueError):
            continue
        if facts.get("NTP_SYNC") != "yes":
            offsets.pop()
    if not offsets or min(abs(offset) for offset in offsets) < CLOCK_SKEW_LIMIT_SECONDS:
        return []
    skew = sorted(offsets)[len(offsets) // 2]
    direction = "chậm" if skew > 0 else "nhanh"
    return [Finding("Ceph AI", "CEPH_AI_CLOCK_SKEW", "warning",
                    f"Đồng hồ máy Ceph AI {direction} {abs(skew):.1f} s so với {len(offsets)} node đã đồng bộ NTP.",
                    "trên máy Ceph AI: systemctl enable --now chronyd; chronyc makestep")]


def fingerprint(findings: list[Finding]) -> str:
    keys = sorted(f"{finding.host}|{finding.code}" for finding in findings)
    return hashlib.sha256("\n".join(keys).encode()).hexdigest()[:16]


def message(findings: list[Finding]) -> str:
    if not findings:
        return "✅ Cấu hình OS các node Ceph: không còn phát hiện nào."
    lines = ["🧰 Cấu hình OS các node Ceph cần xem:"]
    for finding in findings:
        icon = "⚠️" if finding.severity == "warning" else "ℹ️"
        lines.append(f"{icon} {finding.host}: {finding.message}\n   → {finding.fix}")
    return "\n".join(lines)


def collect(nodes: list[str]) -> dict[str, dict[str, str]]:
    """Facts per reachable node; an unreachable node is skipped, never raised."""
    from watcher import ceph_client

    facts: dict[str, dict[str, str]] = {}
    for host in nodes:
        started = time.time()
        try:
            output = ceph_client.run_command_on_node(host, FACTS_COMMAND, timeout=FACTS_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.warning("node config audit: %s unavailable: %s", host, exc)
            continue
        facts[host] = parse_facts(output)
        # The node's clock is read somewhere inside the SSH round trip: compare with its midpoint.
        facts[host]["_LOCAL_EPOCH"] = f"{(started + time.time()) / 2:.3f}"
    return facts


def osd_memory_target() -> int | None:
    from watcher import ceph_client

    try:
        _host, output = ceph_client.run_ceph_text_command("ceph config get osd osd_memory_target")
    except Exception as exc:
        logger.warning("node config audit: osd_memory_target unavailable: %s", exc)
        return None
    value = str(output or "").strip().splitlines()[-1:] or [""]
    return int(value[0]) if value[0].isdigit() else None


def run_audit(cluster_id: str | None, nodes: list[str], *, notify) -> list[Finding]:
    """Collect, evaluate, publish; call ``notify(text)`` only when the findings changed."""
    from shared.cluster_snapshot import publish_section_snapshot, read_section_snapshot

    facts = collect(nodes)
    if not facts or not cluster_id:
        return []
    findings = evaluate(facts, osd_memory_target())
    current = fingerprint(findings)
    previous = (read_section_snapshot(cluster_id, SECTION, max_stale_seconds=30 * 86400) or {}).get(SECTION)
    publish_section_snapshot(cluster_id, SECTION, {
        "fingerprint": current,
        "nodes": len(facts),
        "findings": [finding.as_dict() for finding in findings],
    }, source="watcher-node-config")
    if not isinstance(previous, dict) or previous.get("fingerprint") != current:
        notify(message(findings))
    return findings

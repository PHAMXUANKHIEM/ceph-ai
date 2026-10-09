"""Why did a node stop answering? Read its kernel journal once it is back.

A NODE_UNREACHABLE incident only says SSH failed. When the host answers
again, one read-only SSH call reads the kernel journal of the previous boot
(the host was reset) or of the outage window (it came back by itself), and
the incident gets a classified post-mortem: soft lockup (and whether memory
reclaim was stuck), hung I/O tasks, OOM, disk I/O errors, a reset without a
kernel trace, or an outage the kernel did not notice (network or hypervisor).

09/10/2026: ceph2 was reset at 09:29 after kswapd0, khugepaged and the OSD's
bstore_kv_sync soft-locked for 24-50 s; finding that took a manual journal
read on the node. Evidence needs a persistent journal (/var/log/journal).
"""

from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

POSTMORTEM_TIMEOUT_SECONDS = 25
POSTMORTEM_EVENT = "node_postmortem"
# A boot this close before detection still counts as "rebooted during the outage".
_REBOOT_SLACK_SECONDS = 120
_MAX_LINES = 40
_PATTERN = "soft lockup|blocked for more than|Out of memory|oom-kill|I/O error|hung_task"
_PROCESS_RE = re.compile(r"\[([A-Za-z0-9_./:-]+?):\d+\]")
_RECLAIM_THREADS = ("kswapd", "khugepaged", "kcompactd")

SOFT_LOCKUP = "KERNEL_SOFT_LOCKUP"
HUNG_TASK = "HUNG_TASK_IO"
OUT_OF_MEMORY = "OUT_OF_MEMORY"
DISK_IO_ERROR = "DISK_IO_ERROR"
RESET_NO_TRACE = "RESET_WITHOUT_KERNEL_TRACE"
REBOOTED_NO_JOURNAL = "REBOOTED_NO_PERSISTENT_JOURNAL"
NO_KERNEL_TRACE = "UNREACHABLE_WITHOUT_KERNEL_TRACE"
UNAVAILABLE = "POSTMORTEM_UNAVAILABLE"


@dataclass(frozen=True)
class Postmortem:
    kind: str
    rebooted: bool | None
    summary: str
    processes: tuple[str, ...] = ()
    evidence: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "rebooted": self.rebooted,
            "summary": self.summary,
            "processes": list(self.processes),
            "evidence": list(self.evidence),
        }


def postmortem_command(outage_started_at: datetime) -> str:
    """One read-only shell snippet; every section is tagged for parse_output()."""
    since = int(_aware(outage_started_at).timestamp()) - 300
    grep = f"grep -E {shlex.quote(_PATTERN)} | tail -n {_MAX_LINES}"
    return " ; ".join((
        'echo "BOOT $(date -u -d "$(uptime -s)" +%s)"',
        'echo "PERSISTENT $([ -d /var/log/journal ] && echo 1 || echo 0)"',
        f"journalctl -k -b -1 --no-pager -o short-iso 2>/dev/null | {grep} | sed 's/^/PREV /'",
        f"journalctl -k -b 0 --since @{since} --no-pager -o short-iso 2>/dev/null | {grep} | sed 's/^/CUR /'",
        "journalctl -b -1 -n 1 --no-pager -o short-iso 2>/dev/null | tail -n 1 | sed 's/^/LAST /'",
        "true",
    ))


def _parse(output: str) -> tuple[int | None, bool, list[str], list[str], str]:
    """(boot epoch, persistent journal, previous-boot lines, outage-window lines, last line)."""
    boot_epoch: int | None = None
    persistent, last_line = False, ""
    sections: dict[str, list[str]] = {"PREV": [], "CUR": []}
    for line in output.splitlines():
        tag, _, rest = line.partition(" ")
        if tag == "BOOT" and rest.strip().isdigit():
            boot_epoch = int(rest.strip())
        elif tag == "PERSISTENT":
            persistent = rest.strip() == "1"
        elif tag in sections:
            sections[tag].append(rest)
        elif tag == "LAST":
            last_line = rest
    return boot_epoch, persistent, sections["PREV"], sections["CUR"], last_line


def _kernel_verdict(lines: list[str], restart: str) -> tuple[str, str] | None:
    """Kind and summary for the kernel's own trouble lines, most specific first."""
    checks = (
        (HUNG_TASK, ("blocked for more than", "hung_task"),
         "Tiến trình bị treo chờ I/O quá 120 s (hung_task): nghi đĩa/volume chậm."),
        (OUT_OF_MEMORY, ("Out of memory", "oom-kill"), "Hết bộ nhớ (OOM killer)."),
        (DISK_IO_ERROR, ("I/O error",), "Lỗi I/O đĩa trong kernel log."),
    )
    for kind, needles, text in checks:
        if any(needle in line for line in lines for needle in needles):
            return kind, f"{text} {restart}"
    return None


def _soft_lockup(lines: list[str], rebooted: bool, restart: str, evidence: tuple[str, ...]) -> Postmortem | None:
    processes = tuple(dict.fromkeys(
        match.group(1) for line in lines if "soft lockup" in line for match in _PROCESS_RE.finditer(line)
    ))
    if not processes:
        return None
    reclaim = [name for name in processes if name.startswith(_RECLAIM_THREADS)]
    cause = f"kẹt trong thu hồi bộ nhớ ({', '.join(reclaim)})" if reclaim else "CPU bị kẹt"
    return Postmortem(SOFT_LOCKUP, rebooted, (
        f"Kernel soft lockup, {cause}; tiến trình bị kẹt: {', '.join(processes)}. {restart}"
    ), processes, evidence)


def classify(output: str, outage_started_at: datetime) -> Postmortem:
    """Turn the command output into a post-mortem; pure, so it is testable offline."""
    boot_epoch, persistent, previous, current, last_line = _parse(output)
    if boot_epoch is None:
        return Postmortem(UNAVAILABLE, None, "Không đọc được thời điểm boot của node.")
    rebooted = boot_epoch > _aware(outage_started_at).timestamp() - _REBOOT_SLACK_SECONDS
    lines = previous if rebooted else current
    evidence = tuple(line[:240] for line in lines[-8:])
    restart = "Sau đó node bị khởi động lại." if rebooted else "Sau đó node tự hồi lại, không reboot."
    lockup = _soft_lockup(lines, rebooted, restart, evidence)
    if lockup is not None:
        return lockup
    verdict = _kernel_verdict(lines, restart)
    if verdict is not None:
        return Postmortem(verdict[0], rebooted, verdict[1], (), evidence)
    return _without_kernel_trace(rebooted, persistent, last_line)


def _without_kernel_trace(rebooted: bool, persistent: bool, last_line: str) -> Postmortem:
    if rebooted and not persistent:
        return Postmortem(REBOOTED_NO_JOURNAL, True, (
            "Node đã reboot nhưng journal không lưu xuống đĩa, nên không còn log của lần boot trước. "
            "Bật /var/log/journal để lần sau có bằng chứng."
        ))
    if rebooted:
        last = f" Dòng log cuối trước reset: {last_line[:160]}" if last_line else ""
        return Postmortem(RESET_NO_TRACE, True, (
            "Node bị reset mà kernel không ghi soft lockup, hung_task hay OOM: nghi reset từ "
            f"hypervisor/console hoặc mất nguồn.{last}"
        ), (), (last_line[:240],) if last_line else ())
    return Postmortem(NO_KERNEL_TRACE, False, (
        "Node tự hồi lại và kernel không ghi lỗi trong lúc mất kết nối: nghi mạng hoặc VM bị tạm dừng."
    ))


def collect(host: str, outage_started_at: datetime) -> Postmortem:
    """Run the read-only journal read on ``host``; never raises."""
    from watcher import ceph_client

    try:
        output = ceph_client.run_command_on_node(
            host, postmortem_command(outage_started_at), timeout=POSTMORTEM_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning("node postmortem for %s unavailable: %s", host, exc)
        return Postmortem(UNAVAILABLE, None, f"Không đọc được journal của node: {str(exc)[:200]}")
    return classify(output, outage_started_at)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value

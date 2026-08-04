"""Epic 10 Story 10.3: Group A (RUN, during-upgrade) test cases from
docs/ceph-upgrade-test-cases.md, section 2. Commands and criteria wording
below come straight from that document.

**Known gap**: the source document jumps TC-RUN-001 -> TC-RUN-004; the
summary table claims 13 Group A test cases but only 11 have supplied
content (TC-RUN-001, TC-RUN-004 through TC-RUN-013). TC-RUN-002 and
TC-RUN-003 are not implemented here -- there is no content to implement
against. Add them here once the user supplies the missing two.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from worker.executor.ssh_executor import ExecutorError, execute_background
from worker.executor.test_runner.framework import (
    CriterionResult,
    TestCase,
    TestCaseError,
    TestGroup,
    TestPriority,
    TestResult,
    TestRunContext,
    TestStatus,
    check_background_handle_health,
    run_ceph_command,
)

__all__ = [
    "OsdConvertTarget",
    "TcRun001ContinuousRbdIo",
    "TcRun004PgStatus",
    "TcRun005MonQuorum",
    "TcRun006SlowOps",
    "TcRun007DaemonDowntime",
    "TcRun008SystemErrorLogs",
    "TcRun009CrashModule",
    "TcRun010OldClientDuringUpgrade",
    "TcRun011PgDegraded",
    "TcRun012NoUnplannedRebalance",
    "TcRun013OmapConvert",
    "GROUP_A_TESTS",
]


def _require_mon_host(ctx: TestRunContext, test_id: str) -> str:
    if not ctx.mon_host:
        raise TestCaseError(f"{test_id}: chua cau hinh MON host nao")
    return ctx.mon_host


def _parse_json(output: str, test_id: str) -> dict:
    try:
        return json.loads(output)
    except (ValueError, TypeError) as exc:
        raise TestCaseError(f"{test_id}: khong doc duoc JSON tu output ceph: {exc}") from exc


class TcRun001ContinuousRbdIo(TestCase):
    """I/O RBD lien tuc khong gian doan. Fires a near-infinite fio job on
    the configured client host and watches its output for I/O/verify
    errors. The 2 baseline-comparison criteria (p99 latency, no >30s
    stall) are NOT automated -- they need the final fio JSON report
    compared against a pre-upgrade baseline number this project doesn't
    collect (Story 10.2's config scope stopped at baseline FILES for
    checksum diffing, not perf numbers) -- left as manual-review criteria.
    """

    id = "TC-RUN-001"
    name = "I/O RBD lien tuc khong gian doan"
    group = TestGroup.A
    priority = TestPriority.P1
    background = True

    CLIENT_DEVICE = "/dev/vde"
    LOG_PATH = "/var/log/fio_upgrade.json"
    ERROR_KEYWORDS = ("input/output error", "verify failed", "fio: pid=")

    def start(self, ctx: TestRunContext, **kwargs) -> Any:
        if not ctx.client_host:
            raise TestCaseError(f"{self.id}: chua cau hinh client_host (Config -> May client test I/O)")
        fio_cmd = (
            "fio --name=upgrade_io_test "
            f"--filename={self.CLIENT_DEVICE} "
            "--ioengine=libaio --direct=1 --rw=randrw --rwmixread=70 "
            "--bs=4k --iodepth=32 --numjobs=4 --time_based --runtime=99999 "
            "--verify=crc32c --verify_fatal=1 --output-format=json "
            f"--output={self.LOG_PATH}"
        )
        handle = execute_background(ctx.client_host, f"bash -c '{fio_cmd}'")
        return {"handle": handle, "error_seen": False}

    def poll(self, ctx: TestRunContext, state: Any) -> tuple[Any, TestResult]:
        health = check_background_handle_health(
            state["handle"], state.get("error_seen", False), self.ERROR_KEYWORDS
        )
        new_state = {"handle": state["handle"], "error_seen": health["error_seen"]}
        criteria = [
            CriterionResult(
                "0 loi Input/output error, 0 loi verify",
                passed=(False if health["error_seen"] else None),
                detail="phat hien loi trong output fio" if health["error_seen"] else "chua phat hien loi",
            ),
            CriterionResult(
                "p99 latency khong vuot qua 3 lan so voi baseline",
                passed=None,
                detail=f"can doi chieu {self.LOG_PATH} voi baseline thu cong sau khi nang cap xong",
            ),
            CriterionResult(
                "Khong co khoang thoi gian I/O bi treo hoan toan qua 30 giay",
                passed=None,
                detail=f"can doi chieu {self.LOG_PATH} voi baseline thu cong sau khi nang cap xong",
            ),
        ]
        result = TestResult(
            test_id=self.id,
            status=TestStatus.PENDING,
            criteria=criteria,
            raw_output=health["new_stdout"] + health["new_stderr"],
        )
        return new_state, result


class TcRun004PgStatus(TestCase):
    id = "TC-RUN-004"
    name = "Giam sat trang thai PG lien tuc"
    group = TestGroup.A
    priority = TestPriority.P1

    BAD_PG_KEYWORDS = ("inactive", "down", "incomplete", "stale")

    def run(self, ctx: TestRunContext) -> TestResult:
        mon_host = _require_mon_host(ctx, self.id)
        output = run_ceph_command(mon_host, "ceph health detail")
        lowered = output.lower()
        found = [kw for kw in self.BAD_PG_KEYWORDS if kw in lowered]
        criteria = [
            CriterionResult(
                "Khong co PG inactive/down/incomplete/stale (ngoai tru degraded tam thoi)",
                passed=not found,
                detail=f"phat hien tu khoa: {', '.join(found)}" if found else "",
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcRun005MonQuorum(TestCase):
    id = "TC-RUN-005"
    name = "Giam sat quorum MON lien tuc"
    group = TestGroup.A
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        mon_host = _require_mon_host(ctx, self.id)
        output = run_ceph_command(mon_host, "ceph quorum_status --format json-pretty")
        data = _parse_json(output, self.id)
        quorum_size = len(data.get("quorum", []) or [])
        criteria = [
            CriterionResult(
                "Quorum khong giam duoi 2/3 (quorum_size >= 2)",
                passed=quorum_size >= 2,
                detail=f"quorum_size hien tai = {quorum_size}",
            ),
            CriterionResult(
                "Thoi gian moi MON quay lai quorum <= 60 giay",
                passed=None,
                detail="can theo doi qua nhieu lan goi lien tiep de do thoi gian rejoin -- xem TC-RUN-007",
            ),
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcRun006SlowOps(TestCase):
    id = "TC-RUN-006"
    name = "Giam sat slow ops"
    group = TestGroup.A
    priority = TestPriority.P2

    def run(self, ctx: TestRunContext) -> TestResult:
        mon_host = _require_mon_host(ctx, self.id)
        output = run_ceph_command(mon_host, "ceph health detail")
        lowered = output.lower()
        has_slow_ops = "slow ops" in lowered or "slow_ops" in lowered
        criteria = [
            CriterionResult(
                "Khong co slow ops tai thoi diem kiem tra",
                passed=not has_slow_ops,
                detail="phat hien SLOW_OPS trong health detail" if has_slow_ops else "",
            ),
            CriterionResult(
                "Moi lan xuat hien ton tai < 60 giay; khong OSD nao lap lai nhieu lan",
                passed=None,
                detail="can lich su nhieu lan goi lien tiep de danh gia thoi luong/tan suat",
            ),
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcRun007DaemonDowntime(TestCase):
    """Do downtime tung daemon khi restart. Unlike the document's manual
    per-daemon `date` bracketing, this polls ceph's own aggregate counters
    (osd up-count / mon quorum size / mgr availability) each tick and infers
    a down->up transition itself -- no operator action required to mark
    stop/start timestamps. MDS is out of scope: shared/cluster_nodes.py's
    configured_nodes() has no MDS role, so there is no host to poll an MDS
    status from without a new config surface this story doesn't add.
    """

    id = "TC-RUN-007"
    name = "Do downtime tung daemon khi restart"
    group = TestGroup.A
    priority = TestPriority.P2
    background = True

    THRESHOLDS_SECONDS = {"osd": 180.0, "mon": 60.0, "mgr": 30.0}
    LABELS = {
        "osd": "OSD downtime moi cai <= 180 giay",
        "mon": "MON join lai quorum <= 60 giay",
        "mgr": "MGR failover sang standby <= 30 giay",
    }

    def start(self, ctx: TestRunContext, **kwargs) -> Any:
        _require_mon_host(ctx, self.id)
        return {
            "down_since": {"osd": None, "mon": None, "mgr": None},
            "downtimes": {"osd": [], "mon": [], "mgr": []},
        }

    def _snapshot(self, ctx: TestRunContext) -> dict:
        mon_host = ctx.mon_host
        osd_stat = _parse_json(run_ceph_command(mon_host, "ceph osd stat --format json"), self.id)
        quorum = _parse_json(run_ceph_command(mon_host, "ceph quorum_status --format json-pretty"), self.id)
        status = _parse_json(run_ceph_command(mon_host, "ceph -s --format json"), self.id)
        num_osds = osd_stat.get("num_osds", 0) or 0
        num_up = osd_stat.get("num_up_osds", 0) or 0
        num_mons = len((quorum.get("monmap") or {}).get("mons", []) or [])
        quorum_size = len(quorum.get("quorum", []) or [])
        mgr_available = bool((status.get("mgrmap") or {}).get("available", True))
        return {
            "osd": num_osds == 0 or num_up >= num_osds,
            "mon": num_mons == 0 or quorum_size >= num_mons,
            "mgr": mgr_available,
        }

    def poll(self, ctx: TestRunContext, state: Any) -> tuple[Any, TestResult]:
        now = datetime.utcnow()
        healthy = self._snapshot(ctx)

        down_since = dict(state["down_since"])
        downtimes = {k: list(v) for k, v in state["downtimes"].items()}
        for daemon, is_healthy in healthy.items():
            if not is_healthy and down_since[daemon] is None:
                down_since[daemon] = now
            elif is_healthy and down_since[daemon] is not None:
                downtimes[daemon].append((now - down_since[daemon]).total_seconds())
                down_since[daemon] = None

        new_state = {"down_since": down_since, "downtimes": downtimes}

        criteria = []
        for daemon in ("osd", "mon", "mgr"):
            threshold = self.THRESHOLDS_SECONDS[daemon]
            recorded = downtimes[daemon]
            still_down = down_since[daemon] is not None
            over = [d for d in recorded if d > threshold]
            if still_down:
                passed = None
                detail = f"dang trong trang thai down tu {down_since[daemon].isoformat()}"
            elif not recorded:
                passed = None
                detail = "chua ghi nhan lan restart nao cua daemon nay"
            else:
                passed = not over
                detail = f"da ghi nhan: {[round(d, 1) for d in recorded]} giay"
            criteria.append(CriterionResult(self.LABELS[daemon], passed=passed, detail=detail))

        result = TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria)
        return new_state, result


class TcRun008SystemErrorLogs(TestCase):
    """Kiem tra log loi he thong tren toan bo node. Polls `journalctl -u
    'ceph-*' --since <last poll>` per node instead of the document's
    persistent `journalctl -f` follow -- same net effect (every new log
    line gets scanned exactly once) without holding an open follow process
    per node for the whole upgrade window.
    """

    id = "TC-RUN-008"
    name = "Kiem tra log loi he thong tren toan bo node"
    group = TestGroup.A
    priority = TestPriority.P1
    background = True

    CRITICAL_KEYWORDS = ("failed ceph_assert", "segmentation fault", "core dumped")

    def _hosts(self, ctx: TestRunContext) -> list[str]:
        hosts: list[str] = []
        if ctx.mon_host:
            hosts.append(ctx.mon_host)
        for h in list(ctx.osd_hosts) + list(ctx.rgw_hosts):
            if h not in hosts:
                hosts.append(h)
        return hosts

    def start(self, ctx: TestRunContext, **kwargs) -> Any:
        hosts = self._hosts(ctx)
        if not hosts:
            raise TestCaseError(f"{self.id}: chua cau hinh node nao (MON/OSD/RGW)")
        return {
            "since": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            "hosts": hosts,
            "critical_hits": [],
        }

    def poll(self, ctx: TestRunContext, state: Any) -> tuple[Any, TestResult]:
        since = state["since"]
        new_since = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        critical_hits = list(state["critical_hits"])
        for host in state["hosts"]:
            try:
                output = run_ceph_command(
                    host, f"journalctl -u 'ceph-*' --since '{since}' -o cat 2>/dev/null || true"
                )
            except ExecutorError as exc:
                critical_hits_note = f"{host}: khong doc duoc journalctl ({exc})"
                critical_hits.append(critical_hits_note)
                continue
            lowered = output.lower()
            for kw in self.CRITICAL_KEYWORDS:
                if kw in lowered:
                    critical_hits.append(f"{host}: {kw}")
        new_state = {"since": new_since, "hosts": state["hosts"], "critical_hits": critical_hits}
        criteria = [
            CriterionResult(
                "0 dong chua FAILED ceph_assert / Segmentation fault / core dumped",
                passed=not critical_hits,
                detail="; ".join(critical_hits) if critical_hits else "",
            ),
            CriterionResult(
                "Moi dong error khac deu tuong ung thoi diem restart/convert du kien",
                passed=None,
                detail="can nguoi van hanh doi chieu voi timeline nang cap thuc te",
            ),
        ]
        result = TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria)
        return new_state, result


class TcRun009CrashModule(TestCase):
    id = "TC-RUN-009"
    name = "Kiem tra crash module"
    group = TestGroup.A
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        mon_host = _require_mon_host(ctx, self.id)
        output = run_ceph_command(mon_host, "ceph crash ls-new")
        lines = [ln for ln in output.strip().splitlines() if ln.strip()]
        has_new_crash = bool(lines) and not (len(lines) == 1 and "entity" in lines[0].lower())
        criteria = [
            CriterionResult(
                "0 crash moi tu luc bat dau den khi hoan tat",
                passed=not has_new_crash,
                detail=output.strip() if has_new_crash else "",
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcRun010OldClientDuringUpgrade(TestCase):
    """Client version cu van hoat dong trong luc nang cap. Reuses the same
    `client_host` as TC-RUN-001 (Story 10.2's config doesn't distinguish a
    separate "old-version client" machine -- a reasonable simplification
    for a single-cluster lab setup; splitting this into 2 config fields is
    a small additive change if a real deployment ever needs it).
    """

    id = "TC-RUN-010"
    name = "Client version cu van hoat dong trong luc nang cap"
    group = TestGroup.A
    priority = TestPriority.P1
    background = True

    RBD_DEVICE = "/dev/rbd1"
    CEPHFS_MOUNT = "/mnt/cephfs_old"
    S3_BUCKET = "upgrade-test-bucket"
    ERROR_KEYWORDS = (
        "input/output error",
        "no such file or directory",
        "nosuchbucket",
        "connection refused",
    )

    def start(self, ctx: TestRunContext, **kwargs) -> Any:
        if not ctx.client_host:
            raise TestCaseError(f"{self.id}: chua cau hinh client_host (Config -> May client test I/O)")
        if not ctx.rgw_endpoint_vip:
            raise TestCaseError(f"{self.id}: chua cau hinh RGW endpoint VIP (Config -> Endpoint RGW)")
        script = (
            "fio --name=old_client_rbd "
            f"--filename={self.RBD_DEVICE} --ioengine=libaio "
            "--rw=randrw --bs=4k --time_based --runtime=99999 --verify=crc32c & "
            f"while true; do echo test > {self.CEPHFS_MOUNT}/f.txt; "
            f"cat {self.CEPHFS_MOUNT}/f.txt >/dev/null; sleep 1; done & "
            f"while true; do aws --endpoint-url {ctx.rgw_endpoint_vip} "
            f"s3 ls s3://{self.S3_BUCKET}/ >/dev/null; sleep 2; done & "
            "wait"
        )
        handle = execute_background(ctx.client_host, f"bash -c '{script}'")
        return {"handle": handle, "error_seen": False}

    def poll(self, ctx: TestRunContext, state: Any) -> tuple[Any, TestResult]:
        health = check_background_handle_health(
            state["handle"], state.get("error_seen", False), self.ERROR_KEYWORDS
        )
        new_state = {"handle": state["handle"], "error_seen": health["error_seen"]}
        criteria = [
            CriterionResult(
                "0 loi tren ca 3 loai tai (RBD/CephFS/S3) trong suot qua trinh",
                passed=(False if health["error_seen"] else None),
                detail="phat hien loi trong output" if health["error_seen"] else "chua phat hien loi",
            )
        ]
        result = TestResult(
            test_id=self.id,
            status=TestStatus.PENDING,
            criteria=criteria,
            raw_output=health["new_stdout"] + health["new_stderr"],
        )
        return new_state, result


class TcRun011PgDegraded(TestCase):
    """Giam sat PG degraded tam thoi. Reports the CURRENT degraded_ratio
    snapshot each poll -- automating the document's exact "<=30 phut ke tu
    luc host hoan tat restart" criterion would require knowing which host's
    restart caused a given degraded episode, which belongs to whatever
    upgrade mechanism the operator actually used (Epic 7 or a fully manual
    process) and that Epic 10 deliberately does not integrate with (see
    epics.md's Epic 10 section). Left as a manual-review criterion.
    """

    id = "TC-RUN-011"
    name = "Giam sat PG degraded tam thoi"
    group = TestGroup.A
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        mon_host = _require_mon_host(ctx, self.id)
        output = run_ceph_command(mon_host, "ceph -s --format json")
        data = _parse_json(output, self.id)
        degraded_ratio = float((data.get("pgmap") or {}).get("degraded_ratio", 0) or 0)
        criteria = [
            CriterionResult(
                "degraded_ratio hien tai = 0 (snapshot tuc thoi)",
                passed=(degraded_ratio == 0),
                detail=f"degraded_ratio = {degraded_ratio}",
            ),
            CriterionResult(
                "Thoi gian tu luc host hoan tat restart den khi degraded ve 0 <= 30 phut",
                passed=None,
                detail="can doi chieu voi timeline nang cap thuc te (ngoai pham vi Epic 10)",
            ),
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcRun012NoUnplannedRebalance(TestCase):
    id = "TC-RUN-012"
    name = "Khong rebalance/backfill ngoai y muon"
    group = TestGroup.A
    priority = TestPriority.P2

    MISPLACED_RATIO_THRESHOLD = 0.05

    def run(self, ctx: TestRunContext) -> TestResult:
        mon_host = _require_mon_host(ctx, self.id)
        status_output = run_ceph_command(mon_host, "ceph -s --format json")
        flags_output = run_ceph_command(mon_host, "ceph osd dump")
        data = _parse_json(status_output, self.id)
        misplaced_ratio = float((data.get("pgmap") or {}).get("misplaced_ratio", 0) or 0)
        noout_set = "noout" in flags_output.lower()
        if noout_set:
            passed = misplaced_ratio <= self.MISPLACED_RATIO_THRESHOLD
            detail = f"misplaced_ratio = {misplaced_ratio}"
        else:
            passed = None
            detail = "co noout chua duoc set -- tieu chi khong ap dung o thoi diem nay"
        criteria = [
            CriterionResult(
                "Khong backfilling/recovering chiem >5% object khi co noout dang set",
                passed=passed,
                detail=detail,
            )
        ]
        return TestResult(
            test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=status_output
        )


@dataclass
class OsdConvertTarget:
    """One OSD TC-RUN-013 must run `ceph-bluestore-tool repair` against.
    `estimated_seconds` is the lab-measured baseline duration from the
    document's precondition step; optional because not every deployment
    will have measured one in advance -- when absent, the "didn't exceed 2x
    estimate" criterion simply has nothing to compare against for that OSD
    and is excluded from the aggregate rather than guessed at.
    """

    osd_id: int
    host: str
    estimated_seconds: Optional[float] = None


class TcRun013OmapConvert(TestCase):
    """Giam sat rieng qua trinh convert OMAP hai lop -- the document's own
    "quan trong nhat" test case. Runs `ceph-bluestore-tool repair` per OSD
    SEQUENTIALLY (matching the document's "cho active+clean truoc khi sang
    OSD tiep theo"), one at a time via execute_background, and PAUSES the
    whole sequence (rather than silently continuing) the moment any OSD
    either fails (non-zero exit) or exceeds 2x its lab estimate -- matching
    the document's explicit "phai canh bao som ... quyet dinh tiep tuc/tam
    dung duoc ghi nhan" requirement, which is a human decision this engine
    does not make for itself. Resuming a paused sequence is Story 10.6/
    10.8's manual-override wiring (FR37), out of this story's scope; this
    class only needs to make the paused state observable.

    Deliberately does NOT stop/start the target OSD itself -- the document
    assumes the OSD is already offline as part of whatever upgrade
    mechanism the operator is running (Epic 7 or fully manual), consistent
    with Epic 10's stated independence from the actual upgrade mechanism.
    """

    id = "TC-RUN-013"
    name = "Giam sat rieng qua trinh convert OMAP hai lop"
    group = TestGroup.A
    priority = TestPriority.P1
    background = True

    def start(self, ctx: TestRunContext, targets: Optional[list[OsdConvertTarget]] = None, **kwargs) -> Any:
        targets = targets or []
        if not targets:
            raise TestCaseError(f"{self.id}: can danh sach OSD can convert (targets) de bat dau")
        return {
            "targets": targets,
            "index": 0,
            "handle": None,
            "current_started_at": None,
            "completed": [],
            "paused": False,
            "pause_reason": "",
        }

    def _launch_current(self, state: dict) -> dict:
        target = state["targets"][state["index"]]
        command = f"bash -c 'ceph-bluestore-tool repair --path /var/lib/ceph/osd/ceph-{target.osd_id} 2>&1'"
        handle = execute_background(target.host, command)
        return {**state, "handle": handle, "current_started_at": datetime.utcnow()}

    def poll(self, ctx: TestRunContext, state: Any) -> tuple[Any, TestResult]:
        if state["handle"] is None and not state["paused"] and state["index"] < len(state["targets"]):
            state = self._launch_current(state)

        new_state = dict(state)
        new_state["completed"] = list(state["completed"])
        raw_output = ""

        if not state["paused"] and state["handle"] is not None:
            handle = state["handle"]
            new_out, new_err = handle.read_new_output()
            raw_output = new_out + new_err
            if handle.is_done():
                exit_code = handle.exit_code()
                duration = (datetime.utcnow() - state["current_started_at"]).total_seconds()
                target = state["targets"][state["index"]]
                over_estimate = (
                    target.estimated_seconds is not None and duration > 2 * target.estimated_seconds
                )
                new_state["completed"].append(
                    {
                        "osd_id": target.osd_id,
                        "seconds": duration,
                        "exit_code": exit_code,
                        "over_estimate": over_estimate,
                    }
                )
                new_state["handle"] = None
                new_state["current_started_at"] = None
                if exit_code != 0 or over_estimate:
                    new_state["paused"] = True
                    reason = f"osd.{target.osd_id}: "
                    reason += f"exit code {exit_code}" if exit_code != 0 else "vuot 2x thoi gian uoc luong"
                    reason += " -- can nguoi van hanh xac nhan truoc khi tiep tuc"
                    new_state["pause_reason"] = reason
                else:
                    new_state["index"] = state["index"] + 1

        completed = new_state["completed"]
        total = len(new_state["targets"])
        all_done = not new_state["paused"] and new_state["index"] >= total
        over_estimate_osds = [c["osd_id"] for c in completed if c["over_estimate"]]
        failed_osds = [c["osd_id"] for c in completed if c["exit_code"] != 0]

        criteria = [
            CriterionResult(
                "Khong OSD nao co thoi gian convert vuot qua 2 lan uoc luong",
                passed=(False if over_estimate_osds else (True if all_done else None)),
                detail=f"vuot nguong: {over_estimate_osds}" if over_estimate_osds else "",
            ),
            CriterionResult(
                "Neu vuot nguong, da canh bao som team van hanh va ghi nhan quyet dinh",
                # Vacuously satisfied once the whole sequence finishes clean
                # (nothing ever exceeded the estimate, so there was nothing
                # to warn about). If an OSD DID exceed it, `paused` blocks
                # `all_done` from ever becoming True without a human
                # resuming the sequence (out of this story's scope) -- so
                # this only reaches True in exactly the case where a human
                # either was never needed or already intervened.
                passed=(True if all_done else None),
                detail=new_state["pause_reason"],
            ),
            CriterionResult(
                "100% OSD convert thanh cong, khong OSD nao phai chay lai repair",
                passed=(False if failed_osds else (True if all_done else None)),
                detail=f"that bai: {failed_osds}" if failed_osds else "",
            ),
        ]
        result = TestResult(
            test_id=self.id,
            status=TestStatus.PENDING,
            criteria=criteria,
            raw_output=raw_output,
            notes=new_state["pause_reason"],
        )
        return new_state, result


GROUP_A_TESTS: list[type[TestCase]] = [
    TcRun001ContinuousRbdIo,
    TcRun004PgStatus,
    TcRun005MonQuorum,
    TcRun006SlowOps,
    TcRun007DaemonDowntime,
    TcRun008SystemErrorLogs,
    TcRun009CrashModule,
    TcRun010OldClientDuringUpgrade,
    TcRun011PgDegraded,
    TcRun012NoUnplannedRebalance,
    TcRun013OmapConvert,
]

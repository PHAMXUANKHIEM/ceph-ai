"""Epic 10 Story 10.5: Group D (performance) test cases from
docs/ceph-upgrade-test-cases.md, section 3.6 (`### 3.6 Hieu nang sau nang
cap`, nested under the document's own "## 3. NHOM B" heading, same
documentation quirk group_c.py's docstring already explains).

**Count**: 7 headings covering 9 IDs -- `TC-PERF-001-003` is written as ONE
combined heading/script/pass-criteria block (IOPS+bandwidth+p99 latency
together), not 3 separate write-ups, so it's implemented as ONE class here
rather than inventing a 3-way split the document doesn't support. Then
TC-PERF-004..009 are each their own class.

**Baseline-comparison criteria are almost all `passed=None` by design**:
every Group D pass criterion is a percentage-degradation check against a
pre-upgrade performance number ("suy giam <= 10%", "tang <= 15%", ...).
Story 10.2's config only stores 7 fixed FILE baselines (checksums, crush
dump, auth list, config dump, df) -- no performance numbers -- the exact
same limitation Story 10.3's TC-RUN-001 already disclosed for its own
baseline-comparison criteria. These test cases still run the REAL
measurement commands and surface the actual numbers in `detail` for a
human to compare against their own recorded baseline; they just can't
auto-judge pass/fail without one.

Uses the shared `require_*`/`run_script`/`parse_steps`/`step_exit_code`
helpers `framework.py` added in Story 10.5 (see group_c.py's docstring).
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Optional

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
    parse_json,
    parse_steps,
    require_client_host,
    require_mon_host,
    run_ceph_command,
    run_script,
    step_exit_code,
)

__all__ = [
    "TcPerf001To003RbdPerformance",
    "TcPerf004ObjectThroughput",
    "TcPerf005RgwPerformance",
    "TcPerf006CephfsMetadata",
    "TcPerf007RecoveryTime",
    "TcPerf008OsdMemory",
    "TcPerf009SoakTest",
    "GROUP_D_TESTS",
]

RBD_POOL = "rbd_rep"
CEPHFS_VERIFY_MOUNT = "/mnt/verify_cephfs"
CEPHFS_ADMIN_SECRET = "/etc/ceph/admin.secret"


def _extract_fio_summary(json_text: str) -> dict:
    """Best-effort extraction of iops/bw/p99 latency from a fio
    --output-format=json report. Deliberately lenient (never raises) --
    this is measurement data for a disclosed manual-comparison criterion,
    not a pass/fail-determining parse, so a shape fio changed across
    versions should degrade to "couldn't extract", not abort the test.

    `json_text` is a `run_script()` step body, which is the raw `cat`'d fio
    JSON file FOLLOWED by that step's own `EXIT:$?` marker line -- extracts
    the `{...}` JSON object by its outermost braces first, since parsing
    the body text as-is would always fail on the trailing "EXIT:N" line
    (`json.loads` rejects any trailing content after a valid object).
    """
    start = json_text.find("{")
    end = json_text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return {}
    try:
        data = json.loads(json_text[start : end + 1])
        job = (data.get("jobs") or [{}])[0]
        read = job.get("read") or {}
        write = job.get("write") or {}
        summary = {}
        for label, side in (("read", read), ("write", write)):
            if side.get("iops"):
                summary[f"{label}_iops"] = side["iops"]
            if side.get("bw"):
                summary[f"{label}_bw_kbps"] = side["bw"]
            p99 = ((side.get("clat_ns") or {}).get("percentile") or {}).get("99.000000")
            if p99:
                summary[f"{label}_p99_latency_ns"] = p99
        return summary
    except (ValueError, TypeError, KeyError, IndexError):
        return {}


class TcPerf001To003RbdPerformance(TestCase):
    id = "TC-PERF-001-003"
    name = "IOPS, bang thong, latency RBD"
    group = TestGroup.D
    priority = TestPriority.P1

    DEVICE = "/dev/rbd6"

    def run(self, ctx: TestRunContext) -> TestResult:
        client = require_client_host(ctx, self.id)
        script = f"""
echo "===STEP:iops==="
fio --name=perf_iops --filename={self.DEVICE} --ioengine=libaio --direct=1 \\
    --rw=randrw --rwmixread=70 --bs=4k --iodepth=32 --numjobs=4 \\
    --time_based --runtime=300 --output-format=json --output=/tmp/perf_iops_after.json 2>&1
cat /tmp/perf_iops_after.json
echo "EXIT:$?"
echo "===STEP:bw==="
fio --name=perf_bw --filename={self.DEVICE} --ioengine=libaio --direct=1 \\
    --rw=write --bs=4M --iodepth=16 --numjobs=1 \\
    --time_based --runtime=300 --output-format=json --output=/tmp/perf_bw_after.json 2>&1
cat /tmp/perf_bw_after.json
echo "EXIT:$?"
"""
        output = run_script(client, script)
        steps = parse_steps(output)
        iops_summary = _extract_fio_summary(steps.get("iops", ""))
        bw_summary = _extract_fio_summary(steps.get("bw", ""))
        criteria = [
            CriterionResult(
                "IOPS/bang thong suy giam <= 10% so voi baseline",
                passed=None,
                detail=f"do duoc: {iops_summary}, {bw_summary} -- can doi chieu thu cong voi baseline da luu rieng",
            ),
            CriterionResult(
                "Latency p99 tang <= 15%",
                passed=None,
                detail=f"p99 hien tai: {iops_summary.get('read_p99_latency_ns')} ns (read) -- can doi chieu voi baseline",
            ),
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcPerf004ObjectThroughput(TestCase):
    id = "TC-PERF-004"
    name = "Throughput object layer"
    group = TestGroup.D
    priority = TestPriority.P2

    BW_RE = re.compile(r"Bandwidth \(MB/sec\):\s*([\d.]+)")

    def run(self, ctx: TestRunContext) -> TestResult:
        mon = require_mon_host(ctx, self.id)
        script = f"""
echo "===STEP:bench==="
rados bench -p {RBD_POOL} 300 write --no-cleanup 2>&1
rados bench -p {RBD_POOL} 300 seq 2>&1
rados bench -p {RBD_POOL} 300 rand 2>&1
rados -p {RBD_POOL} cleanup 2>&1
echo "EXIT:$?"
"""
        output = run_script(mon, script)
        steps = parse_steps(output)
        body = steps.get("bench", "")
        bandwidths = self.BW_RE.findall(body)
        criteria = [
            CriterionResult(
                "Suy giam <= 10% so voi baseline",
                passed=None,
                detail=f"bandwidth (MB/sec) do duoc qua tung giai doan write/seq/rand: {bandwidths} -- can doi chieu voi baseline",
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcPerf005RgwPerformance(TestCase):
    """Hieu nang RGW. The document's own 4h `warp` run is far too long for
    a blocking one-shot SSH call (execute_with_retry's command timeout is
    1800s) -- background start()/poll() shape, same as any other
    multi-hour load in this project. `--access-key`/`--secret-key` are
    dropped from the literal command (the document's placeholders) -- relies
    on client_host's own pre-configured AWS credential chain, the same
    convention Story 10.4's aws-CLI-based S3 test cases already use.
    """

    id = "TC-PERF-005"
    name = "Hieu nang RGW"
    group = TestGroup.D
    priority = TestPriority.P2
    background = True

    LOG_PATH = "/tmp/warp_perf_after.log"
    BUCKET = "perf-test"

    def start(self, ctx: TestRunContext, **kwargs):
        client = require_client_host(ctx, self.id)
        if not ctx.rgw_endpoint_vip:
            raise TestCaseError(f"{self.id}: chua cau hinh RGW endpoint VIP (Config -> Endpoint RGW)")
        host_port = ctx.rgw_endpoint_vip.replace("http://", "").replace("https://", "")
        cmd = (
            f"warp mixed --host={host_port} --bucket={self.BUCKET} "
            f"--duration=4h --concurrent=50 --obj.size=1MiB > {self.LOG_PATH} 2>&1"
        )
        handle = execute_background(client, f"bash -c '{cmd}'")
        return {"handle": handle, "client_host": client}

    def poll(self, ctx: TestRunContext, state):
        handle = state["handle"]
        done = handle.is_done()
        exit_code = handle.exit_code() if done else None
        log_tail = ""
        if done:
            try:
                log_tail = run_ceph_command(state["client_host"], f"tail -c 2000 {self.LOG_PATH} 2>/dev/null")
            except ExecutorError:
                # Best-effort log fetch -- a transient SSH failure here shouldn't
                # fail the whole poll, just leave log_tail empty for this tick.
                log_tail = ""
        failed = done and exit_code not in (0, None)
        if failed:
            detail = f"warp thoat voi exit code {exit_code}: {log_tail}"
        elif done:
            detail = log_tail or "warp hoan tat nhung khong doc duoc log"
        else:
            detail = "dang chay warp benchmark (~4h)"
        criteria = [
            CriterionResult("Suy giam <= 15% so voi baseline", passed=(False if failed else None), detail=detail)
        ]
        result = TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=log_tail)
        return state, result


class TcPerf006CephfsMetadata(TestCase):
    id = "TC-PERF-006"
    name = "Metadata CephFS"
    group = TestGroup.D
    priority = TestPriority.P2

    def run(self, ctx: TestRunContext) -> TestResult:
        client = require_client_host(ctx, self.id)
        script = f"""
echo "===STEP:mdtest==="
mdtest -d {CEPHFS_VERIFY_MOUNT}/mdtest_dir -n 10000 -i 3 2>&1
echo "EXIT:$?"
"""
        output = run_script(client, script)
        steps = parse_steps(output)
        body = steps.get("mdtest", "")
        exit_code = step_exit_code(body)
        if exit_code not in (0, None):
            passed, detail = False, f"lenh mdtest that bai (exit={exit_code}): {body.strip()}"
        else:
            passed, detail = None, (body.strip() or "khong co output") + " -- can doi chieu voi baseline"
        criteria = [CriterionResult("Suy giam <= 15% so voi baseline", passed=passed, detail=detail)]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcPerf007RecoveryTime(TestCase):
    """Thoi gian recovery. The document's own script stops
    `ceph-osd.target` on a node and waits for HEALTH_OK, but never shows a
    step restarting it -- almost certainly an incomplete snippet (leaving
    OSDs stopped indefinitely isn't a meaningful "recovery time"
    measurement, and would be an operational risk for this engine to
    reproduce literally). Deviates from the literal document: always
    restarts `ceph-osd.target` on the SAME target host once either
    HEALTH_OK is reached or MAX_WAIT_SECONDS elapses, whichever comes
    first -- so this test case never leaves a node's OSDs down as a side
    effect of running a diagnostic.
    """

    id = "TC-PERF-007"
    name = "Thoi gian recovery"
    group = TestGroup.D
    priority = TestPriority.P2
    background = True

    MAX_WAIT_SECONDS = 3600

    def start(self, ctx: TestRunContext, **kwargs):
        mon = require_mon_host(ctx, self.id)
        if not ctx.osd_hosts:
            raise TestCaseError(f"{self.id}: chua cau hinh OSD host nao")
        target_host = ctx.osd_hosts[0]
        run_ceph_command(target_host, "systemctl stop ceph-osd.target")
        return {"start_time": datetime.utcnow(), "target_host": target_host, "restarted": False, "mon": mon}

    def poll(self, ctx: TestRunContext, state):
        mon = state["mon"]
        elapsed = (datetime.utcnow() - state["start_time"]).total_seconds()
        status_output = run_ceph_command(mon, "ceph -s --format json")
        data = parse_json(status_output, self.id)
        healthy = (data.get("health") or {}).get("status") == "HEALTH_OK"
        timed_out = elapsed >= self.MAX_WAIT_SECONDS

        new_state = dict(state)
        if (healthy or timed_out) and not state["restarted"]:
            run_ceph_command(state["target_host"], "systemctl start ceph-osd.target")
            new_state["restarted"] = True

        if healthy:
            passed, detail = None, f"recovery_seconds={elapsed:.0f} -- can doi chieu voi baseline"
        elif timed_out:
            passed, detail = False, f"vuot qua {self.MAX_WAIT_SECONDS}s ma chua ve HEALTH_OK -- da khoi dong lai ceph-osd.target"
        else:
            passed, detail = None, f"dang cho HEALTH_OK, da troi qua {elapsed:.0f}s"

        criteria = [CriterionResult("Khong cham hon baseline qua 20%", passed=passed, detail=detail)]
        result = TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=status_output)
        return new_state, result


class TcPerf008OsdMemory(TestCase):
    """Muc tieu thu RAM OSD. The document's own example hardcodes `osd.0`
    (not a placeholder like `<id>`) -- a read-only diagnostic check, safe
    to automate literally against that one example id, same fixed-path
    philosophy as Group A's hardcoded device/mount constants.
    """

    id = "TC-PERF-008"
    name = "Muc tieu thu RAM OSD"
    group = TestGroup.D
    priority = TestPriority.P2

    OSD_ID = "osd.0"

    def run(self, ctx: TestRunContext) -> TestResult:
        mon = require_mon_host(ctx, self.id)
        mempool_output = run_ceph_command(mon, f"ceph daemon {self.OSD_ID} dump_mempools")
        target_output = run_ceph_command(mon, f"ceph config get {self.OSD_ID} osd_memory_target")
        try:
            mempool = parse_json(mempool_output, self.id)
            total_bytes = ((mempool.get("mempool") or {}).get("total_bytes"))
        except TestCaseError:
            total_bytes = None
        target_bytes: Optional[float]
        try:
            target_bytes = float(target_output.strip())
        except (ValueError, TypeError):
            target_bytes = None
        over_by_pct = None
        if total_bytes is not None and target_bytes:
            over_by_pct = (total_bytes - target_bytes) / target_bytes * 100
        criteria = [
            CriterionResult(
                "Khong vuot osd_memory_target qua 20%",
                passed=(over_by_pct <= 20) if over_by_pct is not None else None,
                detail=f"total_bytes={total_bytes}, osd_memory_target={target_bytes}, vuot={over_by_pct}",
            )
        ]
        return TestResult(
            test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=mempool_output + "\n" + target_output
        )


class TcPerf009SoakTest(TestCase):
    """On dinh dai han (soak test). Genuinely 72h background monitoring --
    launches a mixed RBD+CephFS+S3 load (same shape as TC-COMPAT-001/
    group_a.py's TC-RUN-010) and tracks 3 sticky signals across polls: any
    new crash (via `ceph crash ls-new` beyond the start()-time baseline,
    same superset-check idea as TC-POST-009), any non-active+clean PG
    state, and a naive monotonic-RAM-increase heuristic (a leak signature)
    sampled from `ceph daemon osd.0 dump_mempools` each poll. All 3 only
    resolve to a final True once the full 72h has genuinely elapsed --
    matches TC-POST-017/TC-RUN-013's "never auto-PASS off partial
    evidence" precedent.
    """

    id = "TC-PERF-009"
    name = "On dinh dai han (soak test)"
    group = TestGroup.D
    priority = TestPriority.P1
    background = True

    SOAK_TARGET_SECONDS = 72 * 3600
    CEPHFS_MOUNT = "/mnt/cephfs_soak"
    RBD_DEVICE = "/dev/rbd7"
    OSD_ID = "osd.0"
    MAX_RAM_SAMPLES = 50

    def start(self, ctx: TestRunContext, **kwargs):
        client = require_client_host(ctx, self.id)
        mon = require_mon_host(ctx, self.id)
        if not ctx.rgw_endpoint_vip:
            raise TestCaseError(f"{self.id}: chua cau hinh RGW endpoint VIP (Config -> Endpoint RGW)")
        script = (
            "fio --name=soak_rbd "
            f"--filename={self.RBD_DEVICE} --ioengine=libaio "
            "--rw=randrw --bs=4k --time_based --runtime=99999 --verify=crc32c & "
            f"while true; do echo test > {self.CEPHFS_MOUNT}/f.txt; "
            f"cat {self.CEPHFS_MOUNT}/f.txt >/dev/null; sleep 1; done & "
            f"while true; do aws --endpoint-url {ctx.rgw_endpoint_vip} "
            "s3 ls s3://soak-test-bucket/ >/dev/null; sleep 2; done & "
            "wait"
        )
        execute_background(client, f"bash -c '{script}'")
        crash_baseline = run_ceph_command(mon, "ceph crash ls-new")
        return {
            "start_time": datetime.utcnow(),
            "mon": mon,
            "crash_baseline_lines": len([ln for ln in crash_baseline.strip().splitlines() if ln.strip()]),
            "crash_seen": False,
            "pg_issue_seen": False,
            "ram_samples": [],
        }

    def poll(self, ctx: TestRunContext, state):
        mon = state["mon"]
        elapsed = (datetime.utcnow() - state["start_time"]).total_seconds()
        soak_complete = elapsed >= self.SOAK_TARGET_SECONDS

        crash_output = run_ceph_command(mon, "ceph crash ls-new")
        crash_lines = len([ln for ln in crash_output.strip().splitlines() if ln.strip()])
        crash_seen = state["crash_seen"] or crash_lines > state["crash_baseline_lines"]

        status_output = run_ceph_command(mon, "ceph -s --format json")
        status_data = parse_json(status_output, self.id)
        pgmap = status_data.get("pgmap") or {}
        num_pgs = pgmap.get("num_pgs", 0) or 0
        by_state = pgmap.get("pgs_by_state") or []
        active_clean = sum(e.get("count", 0) for e in by_state if e.get("state_name") == "active+clean")
        pg_issue_seen = state["pg_issue_seen"] or (num_pgs > 0 and active_clean != num_pgs)

        ram_samples = list(state["ram_samples"])
        try:
            mempool = parse_json(run_ceph_command(mon, f"ceph daemon {self.OSD_ID} dump_mempools"), self.id)
            total_bytes = (mempool.get("mempool") or {}).get("total_bytes")
            if total_bytes is not None:
                ram_samples.append(total_bytes)
        except TestCaseError:
            pass
        ram_samples = ram_samples[-self.MAX_RAM_SAMPLES :]
        monotonic_increase = len(ram_samples) >= 5 and all(
            ram_samples[i] <= ram_samples[i + 1] for i in range(len(ram_samples) - 1)
        )

        new_state = {
            **state,
            "crash_seen": crash_seen,
            "pg_issue_seen": pg_issue_seen,
            "ram_samples": ram_samples,
        }

        criteria = [
            CriterionResult(
                "Khong crash trong 72 gio",
                passed=(False if crash_seen else (True if soak_complete else None)),
                detail=f"da troi qua {elapsed / 3600:.1f}h / 72h" + (" -- phat hien crash moi" if crash_seen else ""),
            ),
            CriterionResult(
                "Khong PG loi phat sinh",
                passed=(False if pg_issue_seen else (True if soak_complete else None)),
                detail=f"{active_clean}/{num_pgs} active+clean" if num_pgs else "",
            ),
            CriterionResult(
                "RAM dao dong on dinh, khong tang don dieu",
                passed=(False if monotonic_increase else (True if soak_complete else None)),
                detail=f"{len(ram_samples)} mau RAM da ghi nhan" + (" -- co dau hieu tang lien tuc" if monotonic_increase else ""),
            ),
        ]
        result = TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=status_output)
        return new_state, result


GROUP_D_TESTS: list[type[TestCase]] = [
    TcPerf001To003RbdPerformance,
    TcPerf004ObjectThroughput,
    TcPerf005RgwPerformance,
    TcPerf006CephfsMetadata,
    TcPerf007RecoveryTime,
    TcPerf008OsdMemory,
    TcPerf009SoakTest,
]

assert len(GROUP_D_TESTS) == 7, "GROUP_D_TESTS phai co dung 7 test case (TC-PERF-001-003, 004..009)"

"""Epic 10 Story 10.4: Group B (POST, post-upgrade) test cases from
docs/ceph-upgrade-test-cases.md, section 3. Commands and criteria wording
below come from that document; deviations (JSON output flags instead of
plain-text `grep`, a base64-piped script instead of literal shell quoting,
etc.) follow the same principle group_a.py already established: same real
`ceph`/`rbd`/`rados`/`s3` operation, different plumbing so a healthy PASS
doesn't accidentally look like an SSH failure.

**Count correction vs Story 10.3's guess**: the source document's Group B
section actually has 41 test cases with real content (TC-POST-001..025,
030..045 -- 026..029 do not exist in the document at all, the same kind of
ID-numbering gap Group A hit with TC-RUN-002/003), not the ~33 Story 10.3's
Next section guessed before anyone had grepped every heading.

**Deliberately NOT automated (raises TestCaseError with an explanation),
8 of the 41** -- every one of these either needs a real browser session
(TC-POST-023: Dashboard login/click-through) or asks the engine to pick a
specific, currently-in-service cluster resource to mutate/destroy that
neither Story 10.2's config nor the source document itself supplies (it
uses placeholders like `<id>`, `/dev/sdX`, `<mon_id>` -- the document
itself expects a human to fill these in): TC-POST-036 (MDS failover -- no
MDS host role in shared/cluster_nodes.py::configured_nodes()),
TC-POST-040/041 (add/remove a specific OSD), TC-POST-042 (remove/re-add the
only configured MON -- self-destructive to this engine's own SSH target),
TC-POST-043 (reboot every configured node), TC-POST-044 (stop 2 specific
OSD daemons backing an EC pool), TC-POST-045 (run unspecified internal
operator scripts this project has no knowledge of). This mirrors Story
6.2's own precedent of rejecting a denylist-based "run any ceph command"
tool for the same blast-radius reason -- see
[[project-ceph-aiops-bmad-status]]. Every OTHER regression test in section
3.4 (030/031/032/033/034/035/037/038/039) is automated for real because it
only touches a throwaway resource this test itself creates and tears down
(a regression pool/image/bucket/user with a name the document itself
gives), not existing production cluster membership.
"""

from __future__ import annotations

import base64
import csv
import difflib
import json
import re
from datetime import datetime
from typing import Optional

from worker.executor.ssh_executor import ExecutorError, execute_with_retry
from worker.executor.test_runner.framework import (
    CriterionResult,
    TestCase,
    TestCaseDeclined,
    TestCaseError,
    TestGroup,
    TestPriority,
    TestResult,
    TestRunContext,
    TestStatus,
    run_ceph_command,
)
from shared.test_runner_baselines import read_baseline_text

__all__ = [
    "TcPost001VersionUniform",
    "TcPost002ClusterHealth",
    "TcPost003PgIntegrity",
    "TcPost004OsdMapStructure",
    "TcPost005CapacityAndObjects",
    "TcPost006CrushMapUnchanged",
    "TcPost007ConfigPreserved",
    "TcPost008AuthKeyringIntact",
    "TcPost009NoNewCrash",
    "TcPost010RbdReplicatedChecksum",
    "TcPost011RbdErasureCodedChecksum",
    "TcPost012RbdSnapshotClone",
    "TcPost013CephfsChecksum",
    "TcPost014CephfsSnapshot",
    "TcPost015S3DataIntegrity",
    "TcPost016S3ObjectVersioning",
    "TcPost017DeepScrubCluster",
    "TcPost018GlobalIdInsecure",
    "TcPost019OmapWarningsGone",
    "TcPost020PgAutoscaler",
    "TcPost021Balancer",
    "TcPost022MgrModules",
    "TcPost023DashboardWorks",
    "TcPost024PrometheusMetrics",
    "TcPost025Telemetry",
    "TcPost030PoolCreateDelete",
    "TcPost031RbdImageCreateDelete",
    "TcPost032RbdSnapshotCloneNew",
    "TcPost033RbdMirroring",
    "TcPost034CephfsReadWrite",
    "TcPost035CephfsMultiMds",
    "TcPost036MdsFailover",
    "TcPost037S3FullCrud",
    "TcPost038RgwAdmin",
    "TcPost039RgwMultisiteSync",
    "TcPost040AddOsd",
    "TcPost041RemoveOsd",
    "TcPost042ReplaceMon",
    "TcPost043RestartCluster",
    "TcPost044ErasureCodeFailure",
    "TcPost045InternalScripts",
    "GROUP_B_TESTS",
]

TARGET_VERSION = "16.2.15"

RBD_REP_POOL = "rbd_rep"
RBD_REP_IMAGES = [f"testimage{i}" for i in range(1, 6)]
RBD_VERIFY_MOUNT = "/mnt/verify_rbd"
RBD_VERIFY_DEVICE = "/dev/rbd0"
RBD_EC_POOL = "rbd_ec"
# The document's own TC-POST-011 text doesn't name a specific EC image (just
# "thay pool bang rbd_ec") -- reuses TC-POST-044's "ec_test_image" name for
# consistency with the one place in the document an EC image IS named.
RBD_EC_IMAGE = "ec_test_image"
CEPHFS_VERIFY_MOUNT = "/mnt/verify_cephfs"
CEPHFS_ADMIN_SECRET = "/etc/ceph/admin.secret"
CEPHFS_EXPECTED_FILE_COUNT = 200050

S3_MANIFEST_VERIFY_BUCKET = "s3-test-bucket"
S3_VERSIONED_BUCKET = "versioned-bucket"
S3_VERSIONED_TESTFILE = "testfile.txt"

REGRESSION_POOL_NAME = "regression_test_pool"
REGRESSION_RBD_IMAGE = "regression_image"
REGRESSION_SNAP_IMAGE = "snap_test"
REGRESSION_S3_BUCKET = "regression-bucket"
REGRESSION_RGW_USER = "regression-user"
MULTISITE_TEST_BUCKET = "sync-test"
MULTISITE_TEST_KEY = "sync-check.txt"

_STEP_RE = re.compile(r"===STEP:(?P<name>[^=\n]+)===\n(?P<body>.*?)(?=\n===STEP:|\Z)", re.S)
_EXIT_RE = re.compile(r"EXIT:(-?\d+)")


# NOTE: framework.py (Story 10.5) added PUBLIC require_mon_host()/require_client_host()/
# require_rgw_host()/parse_json()/run_script()/parse_steps()/step_exit_code() with
# identical behavior to this module's own private copies below, once group_c.py/
# group_d.py needed the same shape a 3rd/4th time. Left as-is here rather than
# migrated (Story 10.5 treated that as an unrequested refactor of already-shipped,
# tested code) -- but a bug fixed in framework.py's copy will NOT automatically
# apply here. If you're fixing one, check the other.
def _require_mon_host(ctx: TestRunContext, test_id: str) -> str:
    if not ctx.mon_host:
        raise TestCaseError(f"{test_id}: chua cau hinh MON host nao")
    return ctx.mon_host


def _require_client_host(ctx: TestRunContext, test_id: str) -> str:
    if not ctx.client_host:
        raise TestCaseError(f"{test_id}: chua cau hinh client_host (Config -> May client test I/O)")
    return ctx.client_host


def _require_rgw_host(ctx: TestRunContext, test_id: str) -> str:
    if not ctx.rgw_hosts:
        raise TestCaseError(f"{test_id}: chua cau hinh RGW host nao")
    return ctx.rgw_hosts[0]


def _parse_json(output: str, test_id: str) -> dict:
    try:
        return json.loads(output)
    except (ValueError, TypeError) as exc:
        raise TestCaseError(f"{test_id}: khong doc duoc JSON tu output ceph: {exc}") from exc


def _require_baseline(key: str, test_id: str) -> str:
    text = read_baseline_text(key)
    if text is None:
        raise TestCaseError(f"{test_id}: chua upload baseline '{key}' (Config -> Baseline)")
    return text


def _diff_lines(baseline_text: str, current_text: str, max_lines: int = 40) -> list[str]:
    diff = list(
        difflib.unified_diff(
            baseline_text.splitlines(), current_text.splitlines(), fromfile="baseline", tofile="current", lineterm=""
        )
    )
    changed = [ln for ln in diff if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))]
    return changed[:max_lines]


_CONFIG_OPTION_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")


def _extract_config_option_names(text: str) -> set[str]:
    """Best-effort extraction of `ceph config dump` OPTION names -- picks
    tokens that look like a snake_case option name (contains an
    underscore) rather than assuming a fixed column position (WHO/MASK/
    LEVEL/OPTION/VALUE column order has shifted across Ceph versions, and
    scope words like "global"/"mon"/"basic"/"advanced" never contain an
    underscore so this heuristic doesn't mistake them for option names).
    """
    return set(_CONFIG_OPTION_RE.findall(text))


def _run_script(host: str, script: str) -> str:
    """Base64-encodes `script` and pipes it through bash on `host`, instead
    of interpolating it into a `bash -c '...'` command string -- avoids
    quoting hazards from commands that contain their own single/double
    quotes (diff output, echoed manifests) nested several layers deep.
    `script` must end with a step that leaves the shell's own exit code 0
    regardless of what any individual step did -- execute_with_retry()
    raises ExecutorError on ANY non-zero exit, so multi-step scripts here
    encode per-step results as parseable `===STEP:name===` / `EXIT:n`
    markers instead (see _parse_steps()/_step_exit_code()), the same
    "don't let an expected non-zero become ExecutorError" principle
    framework.py's run_ceph_command() already applies to single commands.
    """
    encoded = base64.b64encode(script.encode()).decode()
    return execute_with_retry(host, f"echo {encoded} | base64 -d | bash")


def _parse_steps(output: str) -> dict[str, str]:
    return {m.group("name").strip(): m.group("body") for m in _STEP_RE.finditer(output)}


def _step_exit_code(body: str) -> Optional[int]:
    m = _EXIT_RE.search(body)
    return int(m.group(1)) if m else None


def _verify_checksum_manifest(host: str, manifest_text: str, directory: str, mount_commands: str = "") -> tuple[str, list[str], int]:
    """Pushes `manifest_text` (a Story 10.2 baseline `sha256sum` manifest)
    to `host` and checks it with `cd <directory> && sha256sum -c` --
    deliberately NOT the source document's literal `sha256sum -c manifest
    --directory=<dir>` (GNU coreutils' sha256sum has no `--directory` flag
    to begin with; `cd` first is the portable equivalent). Returns (raw
    output, list of failed/missing-file lines, count of lines that reported
    OK).
    """
    encoded = base64.b64encode(manifest_text.encode()).decode()
    script = f"""
{mount_commands}
echo "===STEP:checksum==="
echo {encoded} | base64 -d > /tmp/test_runner_verify_manifest.sha256
mkdir -p {directory} 2>&1
cd {directory} && sha256sum -c /tmp/test_runner_verify_manifest.sha256 2>&1
echo "EXIT:$?"
"""
    output = _run_script(host, script)
    steps = _parse_steps(output)
    body = steps.get("checksum", output)
    lines = [ln for ln in body.splitlines() if ln.strip() and not ln.startswith("EXIT:")]
    failed = [ln for ln in lines if ln.rstrip().endswith("FAILED") or "No such file or directory" in ln]
    ok_count = sum(1 for ln in lines if ln.rstrip().endswith(": OK"))
    return output, failed, ok_count


# ---------------------------------------------------------------------------
# 3.1 Trang thai cum (TC-POST-001..009)
# ---------------------------------------------------------------------------


class TcPost001VersionUniform(TestCase):
    id = "TC-POST-001"
    name = "Xac nhan version dong nhat"
    group = TestGroup.B
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        mon = _require_mon_host(ctx, self.id)
        output = run_ceph_command(mon, "ceph versions -f json")
        data = _parse_json(output, self.id)
        daemon_types = [k for k in data if k != "overall"]
        mismatched = {}
        for daemon_type in daemon_types:
            versions_map = data.get(daemon_type) or {}
            if len(versions_map) != 1 or TARGET_VERSION not in next(iter(versions_map), ""):
                mismatched[daemon_type] = list(versions_map.keys())
        osd_versions = data.get("osd") or {}
        osd_mismatched = [v for v in osd_versions if TARGET_VERSION not in v]
        criteria = [
            CriterionResult(
                f"`ceph versions` chi liet ke 1 version = {TARGET_VERSION} cho moi loai daemon",
                passed=not mismatched,
                detail=f"khac biet: {mismatched}" if mismatched else "",
            ),
            CriterionResult(
                f"`ceph tell osd.* version` khong con version nao khac {TARGET_VERSION}",
                passed=not osd_mismatched,
                detail=f"osd version le: {osd_mismatched}" if osd_mismatched else "",
            ),
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcPost002ClusterHealth(TestCase):
    id = "TC-POST-002"
    name = "Suc khoe tong the cum"
    group = TestGroup.B
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        mon = _require_mon_host(ctx, self.id)
        status_output = run_ceph_command(mon, "ceph -s --format json")
        detail_output = run_ceph_command(mon, "ceph health detail")
        data = _parse_json(status_output, self.id)
        health_status = (data.get("health") or {}).get("status")
        if health_status == "HEALTH_OK":
            passed, detail = True, ""
        elif health_status == "HEALTH_WARN":
            passed, detail = None, "HEALTH_WARN -- can doi chieu tung dong voi cac test case muc 3.3"
        else:
            passed, detail = False, f"health status = {health_status}"
        criteria = [
            CriterionResult(
                "`ceph -s` = HEALTH_OK, hoac moi dong HEALTH_WARN da map voi 1 test case cu the o muc 3.3",
                passed=passed,
                detail=detail,
            )
        ]
        return TestResult(
            test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=status_output + "\n" + detail_output
        )


class TcPost003PgIntegrity(TestCase):
    id = "TC-POST-003"
    name = "PG toan ven"
    group = TestGroup.B
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        mon = _require_mon_host(ctx, self.id)
        status_output = run_ceph_command(mon, "ceph -s --format json")
        data = _parse_json(status_output, self.id)
        pgmap = data.get("pgmap") or {}
        num_pgs = pgmap.get("num_pgs", 0) or 0
        by_state = pgmap.get("pgs_by_state") or []
        active_clean = sum(e.get("count", 0) for e in by_state if e.get("state_name") == "active+clean")
        stuck_output = run_ceph_command(mon, "ceph pg dump_stuck")
        stuck_stripped = stuck_output.strip().lower()
        criteria = [
            CriterionResult(
                "100% PG active+clean",
                passed=(num_pgs > 0 and active_clean == num_pgs),
                detail=f"{active_clean}/{num_pgs} active+clean",
            ),
            CriterionResult(
                "`pg dump_stuck` tra ve rong",
                passed=stuck_stripped in ("", "ok"),
                detail=stuck_output.strip() if stuck_stripped not in ("", "ok") else "",
            ),
        ]
        return TestResult(
            test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=status_output + "\n" + stuck_output
        )


class TcPost004OsdMapStructure(TestCase):
    """OSD map dung cau truc. `ceph osd crush dump` is naturally JSON
    (matching the baseline's own format) so both halves of this test
    (OSD up/in count vs baseline device count, and the CRUSH diff) come
    from one baseline file. Requires the `osd_crush_dump_before.json`
    baseline -- without it there is no "baseline" to compare either
    criterion against, so a missing upload is an unmet precondition
    (TestCaseError), not a `passed=None` criterion.
    """

    id = "TC-POST-004"
    name = "OSD map dung cau truc"
    group = TestGroup.B
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        mon = _require_mon_host(ctx, self.id)
        baseline_text = _require_baseline("osd_crush_dump_before.json", self.id)
        baseline_json = _parse_json(baseline_text, self.id)
        baseline_osd_count = len(baseline_json.get("devices") or [])

        osd_stat = _parse_json(run_ceph_command(mon, "ceph osd stat --format json"), self.id)
        num_up = osd_stat.get("num_up_osds", 0) or 0
        num_in = osd_stat.get("num_in_osds", 0) or 0

        live_crush_text = run_ceph_command(mon, "ceph osd crush dump")
        diff = _diff_lines(baseline_text, live_crush_text)

        criteria = [
            CriterionResult(
                "Tong so OSD up/in = baseline",
                passed=(num_up == baseline_osd_count and num_in == baseline_osd_count),
                detail=f"baseline={baseline_osd_count}, up={num_up}, in={num_in}",
            ),
            CriterionResult(
                "diff voi CRUSH dump baseline khong co sai khac ngoai du kien",
                passed=(True if not diff else None),
                detail="\n".join(diff) if diff else "",
            ),
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=live_crush_text)


class TcPost005CapacityAndObjects(TestCase):
    """Dung luong & so luong object. The document's exact pass criterion
    ("so object moi pool = baseline + so luong ghi them du kien tu nhom A")
    needs Group A's actual write counts, which this project doesn't
    persist yet (Story 10.8's job) -- left `passed=None` on any diff found;
    only an EMPTY diff against the `df_before.txt` baseline auto-resolves
    True. Uses the same plain `ceph df detail` (no --format) the document's
    own baseline-capture command used, so the diff is comparing like with
    like.
    """

    id = "TC-POST-005"
    name = "Dung luong & so luong object"
    group = TestGroup.B
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        mon = _require_mon_host(ctx, self.id)
        baseline_text = _require_baseline("df_before.txt", self.id)
        live_text = run_ceph_command(mon, "ceph df detail")
        diff = _diff_lines(baseline_text, live_text)
        criteria = [
            CriterionResult(
                "So object moi pool = baseline + ghi them du kien tu nhom A, khong chenh lech am/bat thuong",
                passed=(True if not diff else None),
                detail="\n".join(diff) if diff else "can Story 10.8's du lieu ghi cua nhom A de tu dong hoa day du",
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=live_text)


class TcPost006CrushMapUnchanged(TestCase):
    id = "TC-POST-006"
    name = "CRUSH map khong doi ngoai du kien"
    group = TestGroup.B
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        mon = _require_mon_host(ctx, self.id)
        baseline_text = _require_baseline("osd_crush_dump_before.json", self.id)
        live_text = run_ceph_command(mon, "ceph osd crush dump")
        diff = _diff_lines(baseline_text, live_text)
        criteria = [
            CriterionResult(
                "diff khong co noi dung khac biet ngoai cac thay doi da len ke hoach",
                passed=(True if not diff else None),
                detail="\n".join(diff) if diff else "",
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=live_text)


class TcPost007ConfigPreserved(TestCase):
    """Cau hinh duoc bao toan. Both document criteria ("bien mat ma khong
    giai thich duoc", "liet ke day du doi ten/deprecated") are inherent
    human-judgment calls -- this only surfaces the raw material (which
    config-dump line-prefixes present in the baseline are absent now) for
    a human to judge, same as TC-RUN-011's degraded_ratio snapshot pattern.
    """

    id = "TC-POST-007"
    name = "Cau hinh duoc bao toan"
    group = TestGroup.B
    priority = TestPriority.P2

    def run(self, ctx: TestRunContext) -> TestResult:
        mon = _require_mon_host(ctx, self.id)
        baseline_text = _require_baseline("config_dump_before.txt", self.id)
        live_text = run_ceph_command(mon, "ceph config dump")
        baseline_keys = _extract_config_option_names(baseline_text)
        live_keys = _extract_config_option_names(live_text)
        removed = sorted(baseline_keys - live_keys)
        criteria = [
            CriterionResult(
                "Khong co config tuy chinh nao bien mat ma khong giai thich duoc",
                passed=None,
                detail=f"key co trong baseline nhung khong con: {removed[:20]}" if removed else "khong phat hien key nao bien mat",
            ),
            CriterionResult(
                "Toan bo doi ten/deprecated tich luy qua 2 the he duoc liet ke day du kem gia tri tuong duong moi",
                passed=None,
                detail="can nguoi van hanh doi chieu thu cong voi release notes Octopus + Pacific",
            ),
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=live_text)


class TcPost008AuthKeyringIntact(TestCase):
    """Auth/keyring nguyen ven. Unlike TC-POST-006's CRUSH diff (doc wording
    allows planned changes), the document's wording here is unconditional
    ("diff khong co sai khac") -- any diff is a hard FAIL, not a
    needs-review None.
    """

    id = "TC-POST-008"
    name = "Auth/keyring nguyen ven"
    group = TestGroup.B
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        mon = _require_mon_host(ctx, self.id)
        baseline_text = _require_baseline("auth_list_before.txt", self.id)
        live_text = run_ceph_command(mon, "ceph auth list")
        diff = _diff_lines(baseline_text, live_text)
        criteria = [
            CriterionResult("diff khong co sai khac", passed=not diff, detail="\n".join(diff) if diff else "")
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=live_text)


class TcPost009NoNewCrash(TestCase):
    """Khong co crash moi. Same command/logic as Group A's TC-RUN-009
    (`ceph crash ls-new`) -- the document deliberately repeats this check
    during AND after the upgrade; `ls-new` reports anything not yet
    archived regardless of when it's called, so re-checking post-upgrade is
    a valid (if overlapping) superset check.
    """

    id = "TC-POST-009"
    name = "Khong co crash moi"
    group = TestGroup.B
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        mon = _require_mon_host(ctx, self.id)
        output = run_ceph_command(mon, "ceph crash ls-new")
        lines = [ln for ln in output.strip().splitlines() if ln.strip()]
        has_new_crash = bool(lines) and not (len(lines) == 1 and "entity" in lines[0].lower())
        criteria = [
            CriterionResult(
                "Khong co crash-id moi so voi truoc khi nang cap",
                passed=not has_new_crash,
                detail=output.strip() if has_new_crash else "",
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


# ---------------------------------------------------------------------------
# 3.2 Toan ven du lieu (TC-POST-010..017)
# ---------------------------------------------------------------------------


def _verify_and_cleanup_rbd_image(client: str, image_label: str, manifest: str, mount_commands: str) -> tuple[str, list[str], int]:
    """Shared per-image body for TC-POST-010/011: runs the checksum
    verification, then ALWAYS attempts cleanup (umount/unmap), and returns
    (raw_output, problems, ok_count) rather than letting either step's
    exception propagate uncaught -- two deliberate choices, both fixing
    real gaps found in code review:

    1. `_verify_checksum_manifest`'s own ExecutorError (a transient SSH
       failure, not a real checksum mismatch) is caught HERE rather than
       left to propagate: for TC-POST-010's 5-image loop, an uncaught
       exception on image 2 would abort the whole run() and discard image
       1's already-completed results. Recorded as a `problems` entry so it
       still counts against the "100% OK" criterion (a checksum we
       couldn't even attempt to verify is not a verified-OK checksum).
    2. Cleanup's own result is no longer masked by a trailing `echo done`
       (which always reports "success" regardless of whether umount/unmap
       actually worked) -- a real cleanup failure is now both caught (so it
       can't itself abort the loop or mask a checksum-step exception via
       Python's finally-block exception-replacement semantics) and
       surfaced as a `problems` entry, since a stuck mapping could affect
       the NEXT image's mount.
    """
    problems: list[str] = []
    output = ""
    ok_count = 0
    try:
        output, failed, ok_count = _verify_checksum_manifest(client, manifest, RBD_VERIFY_MOUNT, mount_commands)
        problems.extend(f"{image_label}: {ln}" for ln in failed)
    except ExecutorError as exc:
        problems.append(f"{image_label}: loi SSH khi kiem tra checksum, bo qua image nay ({exc})")
    try:
        cleanup_output = _run_script(
            client, f"umount {RBD_VERIFY_MOUNT} 2>&1; rbd unmap {RBD_VERIFY_DEVICE} 2>&1; echo \"EXIT:$?\""
        )
        if _step_exit_code(cleanup_output) not in (0, None):
            problems.append(f"{image_label}: don dep sau kiem tra that bai (umount/unmap) -- co the anh huong image tiep theo")
    except ExecutorError as exc:
        problems.append(f"{image_label}: loi SSH khi don dep (umount/unmap): {exc}")
    return output, problems, ok_count


class TcPost010RbdReplicatedChecksum(TestCase):
    """Toan ven du lieu RBD replicated. Maps+mounts each of the 5 baseline
    images in turn (reusing one mount point/device, matching the document's
    "lap lai cho toan bo 5 image baseline" instruction), running the SAME
    uploaded `rbd_rep.sha256` manifest against each mount via
    _verify_checksum_manifest() (see that helper's docstring for why `cd +
    sha256sum -c` replaces the document's literal `--directory=` flag,
    which plain sha256sum doesn't actually have). Per-image verify+cleanup
    is isolated via _verify_and_cleanup_rbd_image() so one image's SSH
    hiccup doesn't discard the other 4 images' results.
    """

    id = "TC-POST-010"
    name = "Toan ven du lieu RBD replicated"
    group = TestGroup.B
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        client = _require_client_host(ctx, self.id)
        manifest = _require_baseline("rbd_rep.sha256", self.id)
        problems: list[str] = []
        ok_total = 0
        raw_chunks = []
        for image in RBD_REP_IMAGES:
            mount_commands = (
                f'rbd map {RBD_REP_POOL}/{image} 2>&1\n'
                f"mount {RBD_VERIFY_DEVICE} {RBD_VERIFY_MOUNT} 2>&1\n"
            )
            output, image_problems, ok_count = _verify_and_cleanup_rbd_image(client, image, manifest, mount_commands)
            raw_chunks.append(f"== {image} ==\n{output}")
            problems.extend(image_problems)
            ok_total += ok_count
        criteria = [
            CriterionResult(
                "`sha256sum -c` tra ve OK cho 100% file, 0 dong FAILED",
                passed=(False if problems else (True if ok_total > 0 else None)),
                detail="; ".join(problems) if problems else (f"{ok_total} file OK" if ok_total else "khong xac minh duoc file nao"),
            )
        ]
        return TestResult(
            test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output="\n".join(raw_chunks)
        )


class TcPost011RbdErasureCodedChecksum(TestCase):
    """Toan ven du lieu RBD erasure coded. Tuong tu TC-POST-010 nhung tren
    pool EC -- document text doesn't name a specific EC image, so this
    reuses the `ec_test_image` name TC-POST-044 gives elsewhere in the same
    document, and reuses the SAME `rbd_rep.sha256` baseline manifest since
    Story 10.2's 7 fixed baseline keys have no dedicated EC-pool manifest
    (disclosed judgment call, not a silent assumption).
    """

    id = "TC-POST-011"
    name = "Toan ven du lieu RBD erasure coded"
    group = TestGroup.B
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        client = _require_client_host(ctx, self.id)
        manifest = _require_baseline("rbd_rep.sha256", self.id)
        mount_commands = (
            f"rbd map {RBD_EC_POOL}/{RBD_EC_IMAGE} 2>&1\n" f"mount {RBD_VERIFY_DEVICE} {RBD_VERIFY_MOUNT} 2>&1\n"
        )
        output, problems, ok_count = _verify_and_cleanup_rbd_image(client, RBD_EC_IMAGE, manifest, mount_commands)
        criteria = [
            CriterionResult(
                "100% checksum khop tren toan bo image thuoc pool EC",
                passed=(False if problems else (True if ok_count > 0 else None)),
                detail="; ".join(problems) if problems else (f"{ok_count} file OK" if ok_count else "khong xac minh duoc file nao"),
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcPost012RbdSnapshotClone(TestCase):
    """Snapshot & clone RBD. `/baseline/clone_reference/` is an
    operator-prepared reference directory that must already exist on
    client_host (a fixed lab path per the document, same category as Group
    A's hardcoded device/mount paths) -- not one of Story 10.2's 7 uploaded
    baseline keys. "rbd snap ls liet ke du snapshot" has no persisted
    expected-snapshot list anywhere in this project, so is left manual;
    rollback-succeeded and diff-empty ARE objectively checkable from the
    script's own exit codes.
    """

    id = "TC-POST-012"
    name = "Snapshot & clone RBD"
    group = TestGroup.B
    priority = TestPriority.P1

    CLONE_REFERENCE_DIR = "/baseline/clone_reference"
    CLONE_MOUNT = "/mnt/verify_clone"
    CLONE_DEVICE = "/dev/rbd1"
    IMAGE = "testimage1"

    def run(self, ctx: TestRunContext) -> TestResult:
        client = _require_client_host(ctx, self.id)
        script = f"""
echo "===STEP:snapls==="
rbd snap ls {RBD_REP_POOL}/{self.IMAGE} 2>&1
echo "EXIT:$?"
echo "===STEP:rollback==="
rbd snap rollback {RBD_REP_POOL}/{self.IMAGE}@snap_baseline 2>&1
echo "EXIT:$?"
echo "===STEP:mapmount==="
rbd map {RBD_REP_POOL}/{self.IMAGE}_clone 2>&1 && mount {self.CLONE_DEVICE} {self.CLONE_MOUNT} 2>&1
echo "EXIT:$?"
echo "===STEP:diff==="
diff -r {self.CLONE_MOUNT} {self.CLONE_REFERENCE_DIR}/ 2>&1
echo "EXIT:$?"
echo "===STEP:children==="
rbd children {RBD_REP_POOL}/{self.IMAGE}@snap_baseline 2>&1
echo "EXIT:$?"
umount {self.CLONE_MOUNT} 2>/dev/null
rbd unmap {self.CLONE_DEVICE} 2>/dev/null
# `true` guarantees the whole script's own exit status stays 0 even if
# cleanup above failed (e.g. nothing was ever mapped because mapmount
# failed) -- otherwise execute_with_retry() would raise ExecutorError over
# a cleanup failure and discard every step result already parsed above.
true
"""
        output = _run_script(client, script)
        steps = _parse_steps(output)
        rollback_exit = _step_exit_code(steps.get("rollback", ""))
        diff_exit = _step_exit_code(steps.get("diff", ""))
        criteria = [
            CriterionResult(
                "`rbd snap ls` liet ke du snapshot da tao o baseline",
                passed=None,
                detail=steps.get("snapls", "").strip(),
            ),
            CriterionResult(
                "`rollback` khong loi",
                passed=(rollback_exit == 0) if rollback_exit is not None else None,
                detail=steps.get("rollback", "").strip(),
            ),
            CriterionResult(
                "`diff -r` khong co khac biet",
                passed=(diff_exit == 0) if diff_exit is not None else None,
                detail=steps.get("diff", "").strip(),
            ),
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcPost013CephfsChecksum(TestCase):
    id = "TC-POST-013"
    name = "Toan ven du lieu CephFS"
    group = TestGroup.B
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        client = _require_client_host(ctx, self.id)
        mon = _require_mon_host(ctx, self.id)
        manifest = _require_baseline("cephfs.sha256", self.id)
        mount_commands = (
            f"mountpoint -q {CEPHFS_VERIFY_MOUNT} || "
            f"mount -t ceph {mon}:/ {CEPHFS_VERIFY_MOUNT} -o name=admin,secretfile={CEPHFS_ADMIN_SECRET} 2>&1\n"
        )
        count_script = f"{mount_commands}\nfind {CEPHFS_VERIFY_MOUNT} -type f | wc -l"
        count_output = _run_script(client, count_script)
        try:
            file_count = int(count_output.strip().splitlines()[-1])
        except (ValueError, IndexError):
            file_count = None

        output, failed, ok_count = _verify_checksum_manifest(client, manifest, CEPHFS_VERIFY_MOUNT, mount_commands)
        criteria = [
            CriterionResult(
                f"So file = baseline ({CEPHFS_EXPECTED_FILE_COUNT} file theo kich ban chuan bi)",
                passed=(file_count == CEPHFS_EXPECTED_FILE_COUNT) if file_count is not None else None,
                detail=f"dem duoc {file_count} file" if file_count is not None else count_output.strip(),
            ),
            CriterionResult(
                "`sha256sum -c` khong co dong FAILED",
                passed=(False if failed else (True if ok_count > 0 else None)),
                detail="; ".join(failed) if failed else (f"{ok_count} file OK" if ok_count else "khong xac minh duoc file nao"),
            ),
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcPost014CephfsSnapshot(TestCase):
    """Snapshot CephFS. The document's own example command references a
    `<file_mau>` placeholder with no baseline checksum stored anywhere in
    this project for that specific file -- lists the snapshot directory for
    a human to inspect rather than guessing which file/checksum to check.
    """

    id = "TC-POST-014"
    name = "Snapshot CephFS"
    group = TestGroup.B
    priority = TestPriority.P2

    def run(self, ctx: TestRunContext) -> TestResult:
        client = _require_client_host(ctx, self.id)
        output = run_ceph_command(client, f"ls {CEPHFS_VERIFY_MOUNT}/.snap/base 2>&1")
        criteria = [
            CriterionResult(
                "Snapshot truy cap duoc, noi dung khop checksum baseline",
                passed=None,
                detail="can nguoi van hanh chon file mau va doi chieu checksum thu cong; xem raw_output cho danh sach snapshot",
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcPost015S3DataIntegrity(TestCase):
    """Toan ven du lieu S3. The document's own `verify_s3_manifest.py`
    script is NOT part of this codebase and is not invented here (disclosed
    gap) -- only the automatable half (bucket stats object count vs the
    uploaded `s3_manifest.csv` baseline's row count) is checked. Bucket
    name is a fixed lab constant (S3_MANIFEST_VERIFY_BUCKET) since the
    document's own example uses an unresolved `<ten_bucket>` placeholder.

    Requires the `s3_manifest.csv` baseline via `_require_baseline()` (a
    missing upload is now TestStatus.ERROR, matching every other
    baseline-dependent Group B test case, instead of silently degrading to
    an ambiguous manual-review criterion). Row-counting uses `csv.Sniffer`
    to detect and exclude a header row -- the manifest is an actual .csv,
    and an operator-produced one plausibly has a header, which a naive
    line-count would previously have counted as an extra "object" and
    reported a false MISMATCH on an otherwise perfectly healthy bucket.
    """

    id = "TC-POST-015"
    name = "Toan ven du lieu S3"
    group = TestGroup.B
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        rgw_host = _require_rgw_host(ctx, self.id)
        manifest_text = _require_baseline("s3_manifest.csv", self.id)
        output = run_ceph_command(rgw_host, f"radosgw-admin bucket stats --bucket={S3_MANIFEST_VERIFY_BUCKET}")
        stats = _parse_json(output, self.id)
        try:
            live_count = (stats.get("usage") or {}).get("rgw.main", {}).get("num_objects")
        except (AttributeError, TypeError):
            live_count = None
        rows = [ln for ln in manifest_text.splitlines() if ln.strip()]
        try:
            has_header = bool(rows) and csv.Sniffer().has_header("\n".join(rows[:10]))
        except csv.Error:
            has_header = False
        expected_count = len(rows) - (1 if has_header else 0)
        criteria = [
            CriterionResult(
                "Khong co dong MISMATCH/MISSING trong report",
                passed=None,
                detail="verify_s3_manifest.py tu tai lieu goc khong thuoc codebase nay -- khong tu dong hoa duoc buoc nay",
            ),
            CriterionResult(
                "So object trong `bucket stats` = baseline",
                passed=(live_count == expected_count) if (live_count is not None and expected_count is not None) else None,
                detail=f"live={live_count}, baseline_manifest_rows={expected_count}",
            ),
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcPost016S3ObjectVersioning(TestCase):
    """Object versioning S3. Bucket/file names are literal in the document
    (not placeholders), so hardcoded; the specific `<version_id_cu>` IS a
    placeholder needing operator-specific knowledge this project has no
    config surface for -- lists available versions rather than guessing
    which one to fetch.
    """

    id = "TC-POST-016"
    name = "Object versioning S3"
    group = TestGroup.B
    priority = TestPriority.P2

    def run(self, ctx: TestRunContext) -> TestResult:
        client = _require_client_host(ctx, self.id)
        output = run_ceph_command(client, f"s3cmd ls --list-versions s3://{S3_VERSIONED_BUCKET}/ 2>&1")
        criteria = [
            CriterionResult(
                "Toan bo version day du nhu baseline, noi dung khop checksum",
                passed=None,
                detail=(
                    "khong co version_id baseline nao duoc luu (placeholder trong tai lieu goc) -- "
                    "can nguoi van hanh chon version_id va doi chieu checksum thu cong"
                ),
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


def _find_pg_stats(data: dict) -> list:
    """`ceph pg dump --format json`'s top-level wrapper key for `pg_stats`
    has varied across Ceph versions/configurations -- rather than guess one
    specific wrapper key name (a prior version of this function guessed
    "pg_map", which doesn't match this codebase's own "pgmap" convention
    used elsewhere for `ceph -s --format json` and was never actually
    verified against real Ceph output), search one level deep for any dict
    value containing a `pg_stats` list, in addition to the top level.
    """
    if isinstance(data.get("pg_stats"), list):
        return data["pg_stats"]
    for value in data.values():
        if isinstance(value, dict) and isinstance(value.get("pg_stats"), list):
            return value["pg_stats"]
    return []


def _pg_deep_scrub_stamps(mon_host: str, test_id: str) -> dict[str, str]:
    data = _parse_json(run_ceph_command(mon_host, "ceph pg dump --format json"), test_id)
    stats = _find_pg_stats(data)
    return {e.get("pgid"): e.get("last_deep_scrub_stamp") for e in stats if e.get("pgid")}


def _parse_ceph_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=None)
        except ValueError:
            continue
    return None


class TcPost017DeepScrubCluster(TestCase):
    """Deep-scrub toan cum -- the document's own "quan trong nhat" test
    case, and the only Group B case genuinely long-running enough (whole
    cluster, potentially hours) to need the start()/poll() background shape
    Story 10.3's TcRun013OmapConvert established, rather than a single
    blocking run(). start() triggers `ceph osd deep-scrub all` and snapshots
    each PG's `last_deep_scrub_stamp`; poll() re-snapshots and counts PGs
    whose stamp moved past the start time. An `inconsistent` sighting is
    sticky (like check_background_handle_health()'s error_seen) -- once
    seen it never resets to healthy on its own, matching the document's own
    "phai dieu tra ky nguyen nhan truoc khi dong test case" requirement,
    which is a human decision this engine does not make for itself.
    """

    id = "TC-POST-017"
    name = "Deep-scrub toan cum"
    group = TestGroup.B
    priority = TestPriority.P1
    background = True

    def start(self, ctx: TestRunContext, **kwargs):
        mon = _require_mon_host(ctx, self.id)
        run_ceph_command(mon, "ceph osd deep-scrub all")
        return {
            "start_time": datetime.utcnow(),
            "baseline_stamps": _pg_deep_scrub_stamps(mon, self.id),
            "inconsistent_seen": False,
        }

    def poll(self, ctx: TestRunContext, state):
        mon = ctx.mon_host
        current_stamps = _pg_deep_scrub_stamps(mon, self.id)
        total = len(current_stamps)
        scrubbed = 0
        for pgid, stamp in current_stamps.items():
            parsed = _parse_ceph_timestamp(stamp)
            if parsed is not None and parsed > state["start_time"]:
                scrubbed += 1
        health_output = run_ceph_command(mon, "ceph health detail")
        inconsistent_now = "inconsistent" in health_output.lower()
        inconsistent_seen = state.get("inconsistent_seen", False) or inconsistent_now
        all_done = total > 0 and scrubbed >= total

        new_state = dict(state)
        new_state["inconsistent_seen"] = inconsistent_seen

        criteria = [
            CriterionResult(
                "`ceph health detail` khong bao inconsistent",
                passed=(False if inconsistent_seen else (True if all_done else None)),
                detail="phat hien PG inconsistent -- can `ceph pg repair` va dieu tra ky nguyen nhan" if inconsistent_seen else "",
            ),
            CriterionResult(
                "100% PG hoan tat deep-scrub it nhat 1 lan ke tu sau nang cap",
                passed=(True if all_done else None),
                detail=f"{scrubbed}/{total} PG da deep-scrub ke tu khi bat dau",
            ),
        ]
        result = TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=health_output)
        return new_state, result


# ---------------------------------------------------------------------------
# 3.3 Canh bao & cau hinh moi sau nang cap (TC-POST-018..025)
# ---------------------------------------------------------------------------


class TcPost018GlobalIdInsecure(TestCase):
    """Siet bao mat global_id. Only applies the mutating `ceph config set`
    step (matching the document's literal command) when the warning is
    actually present -- avoids touching a security-relevant global setting
    on a cluster that doesn't need it, a narrower version of Group A's
    TC-RUN-013 precedent of running a real mutating command straight from
    the document.
    """

    id = "TC-POST-018"
    name = "Siet bao mat global_id"
    group = TestGroup.B
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        mon = _require_mon_host(ctx, self.id)
        before = run_ceph_command(mon, "ceph health detail")
        had_warning = "global_id" in before.lower()
        after = before
        if had_warning:
            run_ceph_command(mon, "ceph config set mon auth_allow_insecure_global_id_reclaim false")
            after = run_ceph_command(mon, "ceph health detail")
        still_warning = "global_id" in after.lower()
        criteria = [
            CriterionResult("Canh bao bien mat", passed=not still_warning, detail=after if still_warning else ""),
            CriterionResult(
                "Khong client nao bi tu choi ket noi sau khi siet",
                passed=None,
                detail="can theo doi client trong mot khoang thoi gian, ngoai pham vi 1 lan kiem tra",
            ),
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=before + "\n" + after)


class TcPost019OmapWarningsGone(TestCase):
    id = "TC-POST-019"
    name = "Canh bao OMAP da het (ca 2 loai)"
    group = TestGroup.B
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        mon = _require_mon_host(ctx, self.id)
        health_output = run_ceph_command(mon, "ceph health detail")
        df_output = run_ceph_command(mon, "ceph df detail")
        lowered = (health_output + df_output).lower()
        has_per_pool = "bluestore_no_per_pool_omap" in lowered
        has_per_pg = "bluestore_no_per_pg_omap" in lowered
        criteria = [
            CriterionResult(
                "Khong con ca BLUESTORE_NO_PER_POOL_OMAP va BLUESTORE_NO_PER_PG_OMAP",
                passed=not (has_per_pool or has_per_pg),
                detail=f"per_pool={has_per_pool}, per_pg={has_per_pg}" if (has_per_pool or has_per_pg) else "",
            )
        ]
        return TestResult(
            test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=health_output + "\n" + df_output
        )


class TcPost020PgAutoscaler(TestCase):
    id = "TC-POST-020"
    name = "pg_autoscaler dung trang thai"
    group = TestGroup.B
    priority = TestPriority.P2

    def run(self, ctx: TestRunContext) -> TestResult:
        mon = _require_mon_host(ctx, self.id)
        output = run_ceph_command(mon, "ceph osd pool autoscale-status -f json")
        pools = _parse_json(output, self.id)
        suggested = [
            p.get("pool_name")
            for p in pools
            if isinstance(p, dict)
            and p.get("pg_num_final")
            and p.get("pg_num")
            and p.get("pg_num_final") != p.get("pg_num")
        ]
        criteria = [
            CriterionResult("Trang thai autoscale moi pool khop baseline", passed=None, detail="khong co baseline autoscale-status duoc luu"),
            CriterionResult(
                "Khong goi y thay doi PG_NUM dot bien chua review",
                passed=(True if not suggested else None),
                detail=f"pool co goi y thay doi PG_NUM: {suggested}" if suggested else "",
            ),
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcPost021Balancer(TestCase):
    id = "TC-POST-021"
    name = "Balancer hoat dong"
    group = TestGroup.B
    priority = TestPriority.P2

    def run(self, ctx: TestRunContext) -> TestResult:
        mon = _require_mon_host(ctx, self.id)
        status_output = run_ceph_command(mon, "ceph balancer status -f json")
        eval_output = run_ceph_command(mon, "ceph balancer eval")
        status = _parse_json(status_output, self.id)
        mode = status.get("mode")
        criteria = [
            CriterionResult("Mode giu nguyen (upmap)", passed=(mode == "upmap") if mode is not None else None, detail=f"mode = {mode}"),
            CriterionResult("Score khong xau di", passed=None, detail="khong co baseline eval score duoc luu de so sanh"),
        ]
        return TestResult(
            test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=status_output + "\n" + eval_output
        )


class TcPost022MgrModules(TestCase):
    """Module MGR. No baseline key among Story 10.2's 7 fixed keys captures
    a pre-upgrade module list, so this is informational-only (raw output
    for manual comparison), same disclosed-gap style as TC-POST-020's
    "khop baseline" criterion.
    """

    id = "TC-POST-022"
    name = "Module MGR"
    group = TestGroup.B
    priority = TestPriority.P2

    def run(self, ctx: TestRunContext) -> TestResult:
        mon = _require_mon_host(ctx, self.id)
        output = run_ceph_command(mon, "ceph mgr module ls")
        criteria = [
            CriterionResult(
                "Moi module truoc nang cap van bat hoac co ghi chu ro ly do doi ten/thay the",
                passed=None,
                detail="khong co baseline danh sach module duoc luu -- can nguoi van hanh doi chieu thu cong",
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcPost023DashboardWorks(TestCase):
    """Dashboard hoat dong. The document's own steps are a manual browser
    login + click-through (Hosts/OSDs/Pools/RGW/Cluster status pages) --
    genuinely not automatable over SSH/ceph CLI without either a real
    browser driver or reimplementing a login+HTTP client against the
    Dashboard's session auth, neither of which this story adds. A shallow
    `curl` reachability check would give false confidence (reachable != a
    logged-in page renders without a 500), so this raises rather than
    faking a criterion.
    """

    id = "TC-POST-023"
    name = "Dashboard hoat dong"
    group = TestGroup.B
    priority = TestPriority.P2

    def run(self, ctx: TestRunContext) -> TestResult:
        raise TestCaseDeclined(
            f"{self.id}: doi hoi thao tac trinh duyet thu cong (dang nhap Dashboard, kiem tra tung "
            "trang) -- khong tu dong hoa qua SSH duoc; danh dau Pass/Fail thu cong qua FR37 manual override"
        )


class TcPost024PrometheusMetrics(TestCase):
    """Prometheus/Grafana. Curls the MGR's own exporter from mon_host on
    localhost:9283 -- assumes MGR is colocated with MON, the same
    assumption Group A's TC-RUN-007 already makes for MGR availability
    checks (shared/cluster_nodes.py has no dedicated MGR host list distinct
    from mon_host in TestRunContext).
    """

    id = "TC-POST-024"
    name = "Prometheus/Grafana"
    group = TestGroup.B
    priority = TestPriority.P2

    def run(self, ctx: TestRunContext) -> TestResult:
        mon = _require_mon_host(ctx, self.id)
        output = run_ceph_command(mon, "curl -s http://localhost:9283/metrics | head -50")
        has_metrics = "# HELP" in output or "# TYPE" in output
        criteria = [
            CriterionResult("Tra ve metric hop le", passed=has_metrics, detail="" if has_metrics else output[:500]),
            CriterionResult(
                "Grafana khong co panel trong do doi ten metric tich luy qua 2 the he",
                passed=None,
                detail="can truy cap Grafana truc tiep, ngoai pham vi kiem tra qua SSH",
            ),
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcPost025Telemetry(TestCase):
    """Telemetry. Deliberately does NOT run `ceph telemetry on` -- the
    document itself marks that as conditional ("neu chap nhan opt-in"), an
    explicit operator-consent step this engine does not make on its own.
    """

    id = "TC-POST-025"
    name = "Telemetry"
    group = TestGroup.B
    priority = TestPriority.P3

    def run(self, ctx: TestRunContext) -> TestResult:
        mon = _require_mon_host(ctx, self.id)
        output = run_ceph_command(mon, "ceph telemetry status")
        criteria = [
            CriterionResult(
                "Trang thai telemetry ro rang, khong treo lo lung",
                passed=bool(output.strip()),
                detail="" if output.strip() else "khong nhan duoc output",
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


# ---------------------------------------------------------------------------
# 3.4 Kiem thu chuc nang / Regression (TC-POST-030..045)
# ---------------------------------------------------------------------------


class TcPost030PoolCreateDelete(TestCase):
    id = "TC-POST-030"
    name = "Tao/xoa pool"
    group = TestGroup.B
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        mon = _require_mon_host(ctx, self.id)
        script = f"""
echo "===STEP:create==="
ceph osd pool create {REGRESSION_POOL_NAME} 32 32 2>&1
ceph osd pool application enable {REGRESSION_POOL_NAME} rbd 2>&1
echo "EXIT:$?"
echo "===STEP:verify==="
ceph osd pool ls detail 2>&1 | grep {REGRESSION_POOL_NAME}
echo "EXIT:$?"
echo "===STEP:delete==="
ceph osd pool delete {REGRESSION_POOL_NAME} {REGRESSION_POOL_NAME} --yes-i-really-really-mean-it 2>&1
echo "EXIT:$?"
"""
        output = _run_script(mon, script)
        steps = _parse_steps(output)
        verify_exit = _step_exit_code(steps.get("verify", ""))
        delete_exit = _step_exit_code(steps.get("delete", ""))
        criteria = [
            CriterionResult(
                "Tao/xoa thanh cong, PG moi chuyen active+clean",
                passed=(verify_exit == 0 and delete_exit == 0) if None not in (verify_exit, delete_exit) else None,
                detail=output,
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcPost031RbdImageCreateDelete(TestCase):
    id = "TC-POST-031"
    name = "Tao/xoa RBD image"
    group = TestGroup.B
    priority = TestPriority.P1

    DEVICE = "/dev/rbd2"
    MOUNT = "/mnt/regression"

    def run(self, ctx: TestRunContext) -> TestResult:
        client = _require_client_host(ctx, self.id)
        script = f"""
echo "===STEP:full==="
rbd create {RBD_REP_POOL}/{REGRESSION_RBD_IMAGE} --size 5G 2>&1
rbd resize {RBD_REP_POOL}/{REGRESSION_RBD_IMAGE} --size 10G 2>&1
rbd map {RBD_REP_POOL}/{REGRESSION_RBD_IMAGE} 2>&1
mkfs.xfs -f {self.DEVICE} 2>&1
mkdir -p {self.MOUNT} 2>&1
mount {self.DEVICE} {self.MOUNT} 2>&1
umount {self.MOUNT} 2>&1
rbd unmap {self.DEVICE} 2>&1
rbd rm {RBD_REP_POOL}/{REGRESSION_RBD_IMAGE} 2>&1
echo "EXIT:$?"
"""
        output = _run_script(client, script)
        steps = _parse_steps(output)
        exit_code = _step_exit_code(steps.get("full", ""))
        criteria = [
            CriterionResult(
                "Toan bo lenh chay thanh cong",
                passed=(exit_code == 0) if exit_code is not None else None,
                detail=output,
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcPost032RbdSnapshotCloneNew(TestCase):
    id = "TC-POST-032"
    name = "RBD snapshot/clone moi"
    group = TestGroup.B
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        mon = _require_mon_host(ctx, self.id)
        script = f"""
echo "===STEP:full==="
rbd create {RBD_REP_POOL}/{REGRESSION_SNAP_IMAGE} --size 5G 2>&1
rbd snap create {RBD_REP_POOL}/{REGRESSION_SNAP_IMAGE}@s1 2>&1
rbd snap protect {RBD_REP_POOL}/{REGRESSION_SNAP_IMAGE}@s1 2>&1
rbd clone {RBD_REP_POOL}/{REGRESSION_SNAP_IMAGE}@s1 {RBD_REP_POOL}/{REGRESSION_SNAP_IMAGE}_clone 2>&1
rbd flatten {RBD_REP_POOL}/{REGRESSION_SNAP_IMAGE}_clone 2>&1
rbd snap unprotect {RBD_REP_POOL}/{REGRESSION_SNAP_IMAGE}@s1 2>&1
rbd snap rm {RBD_REP_POOL}/{REGRESSION_SNAP_IMAGE}@s1 2>&1
rbd rm {RBD_REP_POOL}/{REGRESSION_SNAP_IMAGE}_clone 2>&1
rbd rm {RBD_REP_POOL}/{REGRESSION_SNAP_IMAGE} 2>&1
echo "EXIT:$?"
"""
        output = _run_script(mon, script)
        steps = _parse_steps(output)
        exit_code = _step_exit_code(steps.get("full", ""))
        criteria = [
            CriterionResult(
                "Toan bo chuoi lenh thanh cong", passed=(exit_code == 0) if exit_code is not None else None, detail=output
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcPost033RbdMirroring(TestCase):
    """RBD mirroring (neu dung). Environment-dependent per the document's
    own "(neu dung)" -- if mirroring isn't configured for the pool, there
    is nothing to evaluate (passed=None, not applicable) rather than a
    false FAIL.
    """

    id = "TC-POST-033"
    name = "RBD mirroring (neu dung)"
    group = TestGroup.B
    priority = TestPriority.P2

    def run(self, ctx: TestRunContext) -> TestResult:
        mon = _require_mon_host(ctx, self.id)
        output = run_ceph_command(mon, f"rbd mirror pool status --verbose --pool {RBD_REP_POOL} 2>&1")
        lowered = output.lower()
        not_configured = "mirroring not enabled" in lowered or "disabled" in lowered
        has_error_state = "error" in lowered
        criteria = [
            CriterionResult(
                "up+replaying cho toan bo image, khong image error",
                passed=(None if not_configured else (not has_error_state)),
                detail="mirroring chua duoc cau hinh cho pool nay" if not_configured else ("phat hien trang thai error" if has_error_state else ""),
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcPost034CephfsReadWrite(TestCase):
    """Ghi/doc CephFS moi. The "io" step tracks write_errors/read_errors
    explicitly per iteration (`|| write_errors=$((write_errors+1))`) instead
    of relying on the loop's/script's own final exit code -- `rm -f` (the
    prior implementation's last command before the EXIT marker) never fails
    on missing files, so a write/read failure partway through the 10000-file
    loop would otherwise be completely invisible to the pass/fail criterion.
    """

    id = "TC-POST-034"
    name = "Ghi/doc CephFS moi"
    group = TestGroup.B
    priority = TestPriority.P1

    FILE_COUNT = 10000

    def run(self, ctx: TestRunContext) -> TestResult:
        client = _require_client_host(ctx, self.id)
        mon = _require_mon_host(ctx, self.id)
        mount_commands = (
            f"mountpoint -q {CEPHFS_VERIFY_MOUNT} || "
            f"mount -t ceph {mon}:/ {CEPHFS_VERIFY_MOUNT} -o name=admin,secretfile={CEPHFS_ADMIN_SECRET} 2>&1"
        )
        script = f"""
{mount_commands}
echo "===STEP:io==="
write_errors=0
for i in $(seq 1 {self.FILE_COUNT}); do
  echo "data-$i" > {CEPHFS_VERIFY_MOUNT}/regression_$i.txt 2>/tmp/test_runner_io_err.log || write_errors=$((write_errors+1))
done
read_errors=0
for i in $(seq 1 {self.FILE_COUNT}); do
  cat {CEPHFS_VERIFY_MOUNT}/regression_$i.txt > /dev/null 2>>/tmp/test_runner_io_err.log || read_errors=$((read_errors+1))
done
rm -f {CEPHFS_VERIFY_MOUNT}/regression_*.txt
echo "write_errors=$write_errors read_errors=$read_errors"
tail -c 2000 /tmp/test_runner_io_err.log 2>/dev/null
rm -f /tmp/test_runner_io_err.log
if [ "$write_errors" -eq 0 ] && [ "$read_errors" -eq 0 ]; then echo "EXIT:0"; else echo "EXIT:1"; fi
echo "===STEP:slowcheck==="
ceph health detail 2>&1 | grep -i "slow metadata"
echo "EXIT:$?"
"""
        output = _run_script(client, script)
        steps = _parse_steps(output)
        io_exit = _step_exit_code(steps.get("io", ""))
        # grep exits 0 when it found a "slow metadata" line (the bad case), 1 when it found nothing.
        has_slow_metadata = _step_exit_code(steps.get("slowcheck", "")) == 0
        criteria = [
            CriterionResult(
                "Toan bo thao tac hoan tat khong loi, khong canh bao slow metadata IO",
                passed=(io_exit == 0 and not has_slow_metadata) if io_exit is not None else None,
                detail=output[-2000:],
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcPost035CephfsMultiMds(TestCase):
    """CephFS multi-MDS. Discovers the fs name live via `ceph fs ls` instead
    of hardcoding one -- no fs_name config field exists in TestRunContext
    and this avoids adding one for a single value discoverable at run time.
    `max_mds 2` is a persistent cluster setting this test does not revert
    afterward (the document doesn't ask it to either) -- a routine,
    reversible MDS-scaling tunable, same risk class as TC-POST-018's config
    set, unlike the topology-mutating ops in 040-044 this story declines to
    automate.
    """

    id = "TC-POST-035"
    name = "CephFS multi-MDS"
    group = TestGroup.B
    priority = TestPriority.P2

    def run(self, ctx: TestRunContext) -> TestResult:
        mon = _require_mon_host(ctx, self.id)
        ls_output = run_ceph_command(mon, "ceph fs ls -f json")
        fs_list = _parse_json(ls_output, self.id)
        try:
            fs_name = fs_list[0]["name"] if fs_list else None
        except (KeyError, IndexError, TypeError):
            fs_name = None
        if not fs_name:
            raise TestCaseError(f"{self.id}: khong tim thay CephFS filesystem nao qua `ceph fs ls`")
        status_before = run_ceph_command(mon, f"ceph fs set {fs_name} max_mds 2 2>&1; ceph fs status {fs_name} -f json")
        status = _parse_json(status_before.strip().splitlines()[-1], self.id) if status_before.strip() else {}
        mds_versions = status.get("mdsmap") or []
        active_ranks = [m for m in mds_versions if m.get("state") == "active"]
        criteria = [
            CriterionResult(
                "2 rank deu up:active, metadata phan bo can bang",
                passed=(len(active_ranks) >= 2) if status_before.strip() else None,
                detail=f"active ranks hien tai: {len(active_ranks)}",
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=status_before)


class TcPost036MdsFailover(TestCase):
    """Failover MDS. shared/cluster_nodes.py::configured_nodes() has no MDS
    role (same limitation Group A's TC-RUN-007 already disclosed for
    downtime tracking) -- there is no host this test could SSH into to stop
    the active MDS daemon, so it raises rather than guessing one of the
    MON/OSD/RGW hosts is also running an MDS.
    """

    id = "TC-POST-036"
    name = "Failover MDS"
    group = TestGroup.B
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        raise TestCaseDeclined(
            f"{self.id}: chua co cau hinh MDS host (Epic 10's config khong co vai tro MDS rieng) -- "
            "khong xac dinh duoc host nao de dung MDS dang active"
        )


class TcPost037S3FullCrud(TestCase):
    id = "TC-POST-037"
    name = "Thao tac S3 day du"
    group = TestGroup.B
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        client = _require_client_host(ctx, self.id)
        if not ctx.rgw_endpoint_vip:
            raise TestCaseError(f"{self.id}: chua cau hinh RGW endpoint VIP (Config -> Endpoint RGW)")
        endpoint = ctx.rgw_endpoint_vip
        script = f"""
echo "===STEP:crud==="
echo "test-content" > /tmp/test_runner_s3_test.txt
aws --endpoint-url {endpoint} s3api put-object --bucket {REGRESSION_S3_BUCKET} --key test.txt --body /tmp/test_runner_s3_test.txt 2>&1
aws --endpoint-url {endpoint} s3api get-object --bucket {REGRESSION_S3_BUCKET} --key test.txt /tmp/test_runner_s3_downloaded.txt 2>&1
aws --endpoint-url {endpoint} s3api list-objects --bucket {REGRESSION_S3_BUCKET} 2>&1
aws --endpoint-url {endpoint} s3api delete-object --bucket {REGRESSION_S3_BUCKET} --key test.txt 2>&1
echo "EXIT:$?"
"""
        output = _run_script(client, script)
        steps = _parse_steps(output)
        exit_code = _step_exit_code(steps.get("crud", ""))
        criteria = [
            CriterionResult(
                "Toan bo thao tac tra ve ma HTTP dung chuan S3; noi dung tai xuong khop upload",
                passed=(exit_code == 0) if exit_code is not None else None,
                detail=output,
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcPost038RgwAdmin(TestCase):
    id = "TC-POST-038"
    name = "Quan tri RGW"
    group = TestGroup.B
    priority = TestPriority.P2

    def run(self, ctx: TestRunContext) -> TestResult:
        rgw_host = _require_rgw_host(ctx, self.id)
        script = f"""
echo "===STEP:admin==="
radosgw-admin user create --uid={REGRESSION_RGW_USER} --display-name="Regression Test" 2>&1
radosgw-admin user info --uid={REGRESSION_RGW_USER} 2>&1
radosgw-admin quota set --uid={REGRESSION_RGW_USER} --max-size=10G --quota-scope=user 2>&1
radosgw-admin bucket link --bucket={REGRESSION_S3_BUCKET} --uid={REGRESSION_RGW_USER} 2>&1
radosgw-admin bucket reshard --bucket={REGRESSION_S3_BUCKET} --num-shards=8 2>&1
echo "EXIT:$?"
"""
        output = _run_script(rgw_host, script)
        steps = _parse_steps(output)
        exit_code = _step_exit_code(steps.get("admin", ""))
        criteria = [
            CriterionResult("Toan bo lenh chay thanh cong", passed=(exit_code == 0) if exit_code is not None else None, detail=output)
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcPost039RgwMultisiteSync(TestCase):
    id = "TC-POST-039"
    name = "RGW multisite sync"
    group = TestGroup.B
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        client = _require_client_host(ctx, self.id)
        rgw_host = _require_rgw_host(ctx, self.id)
        if not ctx.rgw_endpoint_zone_a or not ctx.rgw_endpoint_zone_b:
            raise TestCaseError(f"{self.id}: chua cau hinh du 2 RGW endpoint zone A/B (Config -> Endpoint RGW)")
        script = f"""
echo "===STEP:putA==="
echo "sync-check" > /tmp/test_runner_sync_check.txt
aws --endpoint-url {ctx.rgw_endpoint_zone_a} s3api put-object --bucket {MULTISITE_TEST_BUCKET} --key {MULTISITE_TEST_KEY} --body /tmp/test_runner_sync_check.txt 2>&1
echo "EXIT:$?"
sleep 60
echo "===STEP:getB==="
aws --endpoint-url {ctx.rgw_endpoint_zone_b} s3api get-object --bucket {MULTISITE_TEST_BUCKET} --key {MULTISITE_TEST_KEY} /tmp/test_runner_synced.txt 2>&1
md5sum /tmp/test_runner_synced.txt /tmp/test_runner_sync_check.txt 2>&1
echo "EXIT:$?"
"""
        output = _run_script(client, script)
        sync_status = run_ceph_command(rgw_host, "radosgw-admin sync status 2>&1")
        steps = _parse_steps(output)
        get_exit = _step_exit_code(steps.get("getB", ""))
        caught_up = "caught up" in sync_status.lower()
        criteria = [
            CriterionResult(
                "Object xuat hien o zone B <= 5 phut, noi dung khop; sync status = caught up",
                passed=(get_exit == 0 and caught_up) if get_exit is not None else None,
                detail=output + "\n" + sync_status,
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output + "\n" + sync_status)


class TcPost040AddOsd(TestCase):
    """Them OSD moi. The document's command targets `/dev/sdX` -- a
    specific spare-disk device path this project has no config surface to
    supply safely (guessing a block device to `ceph-volume lvm create
    --data` against would risk destroying an in-use disk)."""

    id = "TC-POST-040"
    name = "Them OSD moi"
    group = TestGroup.B
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        raise TestCaseDeclined(
            f"{self.id}: can chi dinh thiet bi dia trong cu the (vd /dev/sdX) -- khong co cau hinh nao "
            "de tu dong chon an toan, tranh nguy co pha huy dia dang dung"
        )


class TcPost041RemoveOsd(TestCase):
    """Xoa OSD. The document's `<id>` placeholder means picking a real,
    in-service OSD to purge -- too destructive to auto-select without
    explicit operator input this project has no config surface for."""

    id = "TC-POST-041"
    name = "Xoa OSD"
    group = TestGroup.B
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        raise TestCaseDeclined(f"{self.id}: can chi dinh OSD id cu the de xoa -- qua rui ro de tu dong chon")


class TcPost042ReplaceMon(TestCase):
    """Thay MON. Removing/re-adding a MON needs a `<mon_id>` -- the only
    candidate this engine knows about is ctx.mon_host itself, and removing
    the ONE configured MON this engine's own SSH commands go through would
    be self-destructive to every other test case in this run."""

    id = "TC-POST-042"
    name = "Thay MON"
    group = TestGroup.B
    priority = TestPriority.P2

    def run(self, ctx: TestRunContext) -> TestResult:
        raise TestCaseDeclined(
            f"{self.id}: can chi dinh mon_id cu the; xoa MON dang duoc engine nay dung lam SSH target se "
            "lam hong moi test case khac trong lan chay nay"
        )


class TcPost043RestartCluster(TestCase):
    """Restart toan cum. Rebooting every configured node is far more
    disruptive than any other automated test case in this project
    (compare: TC-RUN-013 only restarts an OSD the real upgrade process
    already took offline) -- this project's own precedent (Story 6.2
    rejecting a denylist-based "run any ceph command" chat tool for blast
    radius reasons) argues against blindly rebooting production-adjacent
    nodes with no explicit human go-ahead."""

    id = "TC-POST-043"
    name = "Restart toan cum"
    group = TestGroup.B
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        raise TestCaseDeclined(
            f"{self.id}: reboot toan bo node la thao tac qua rui ro de tu dong hoa khong co xac nhan "
            "tuong minh cua nguoi van hanh -- can FR37 manual override"
        )


class TcPost044ErasureCodeFailure(TestCase):
    """Erasure code hoat dong. The document's `<id1>`/`<id2>` placeholders
    mean stopping 2 specific real OSD daemons backing an EC pool -- same
    "can't safely auto-select a destructive target" reasoning as
    TC-POST-041."""

    id = "TC-POST-044"
    name = "Erasure code hoat dong"
    group = TestGroup.B
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        raise TestCaseDeclined(
            f"{self.id}: can chi dinh 2 OSD id cu the thuoc pool EC de dung -- qua rui ro de tu dong chon"
        )


class TcPost045InternalScripts(TestCase):
    """Script van hanh noi bo. By definition these are the OPERATOR's own
    monitoring/backup/report scripts, unknown to this project."""

    id = "TC-POST-045"
    name = "Script van hanh noi bo"
    group = TestGroup.B
    priority = TestPriority.P2

    def run(self, ctx: TestRunContext) -> TestResult:
        raise TestCaseDeclined(
            f"{self.id}: script van hanh noi bo cua tung don vi trien khai khong thuoc pham vi du an nay -- "
            "can nguoi van hanh tu chay va doi chieu"
        )


GROUP_B_TESTS: list[type[TestCase]] = [
    TcPost001VersionUniform,
    TcPost002ClusterHealth,
    TcPost003PgIntegrity,
    TcPost004OsdMapStructure,
    TcPost005CapacityAndObjects,
    TcPost006CrushMapUnchanged,
    TcPost007ConfigPreserved,
    TcPost008AuthKeyringIntact,
    TcPost009NoNewCrash,
    TcPost010RbdReplicatedChecksum,
    TcPost011RbdErasureCodedChecksum,
    TcPost012RbdSnapshotClone,
    TcPost013CephfsChecksum,
    TcPost014CephfsSnapshot,
    TcPost015S3DataIntegrity,
    TcPost016S3ObjectVersioning,
    TcPost017DeepScrubCluster,
    TcPost018GlobalIdInsecure,
    TcPost019OmapWarningsGone,
    TcPost020PgAutoscaler,
    TcPost021Balancer,
    TcPost022MgrModules,
    TcPost023DashboardWorks,
    TcPost024PrometheusMetrics,
    TcPost025Telemetry,
    TcPost030PoolCreateDelete,
    TcPost031RbdImageCreateDelete,
    TcPost032RbdSnapshotCloneNew,
    TcPost033RbdMirroring,
    TcPost034CephfsReadWrite,
    TcPost035CephfsMultiMds,
    TcPost036MdsFailover,
    TcPost037S3FullCrud,
    TcPost038RgwAdmin,
    TcPost039RgwMultisiteSync,
    TcPost040AddOsd,
    TcPost041RemoveOsd,
    TcPost042ReplaceMon,
    TcPost043RestartCluster,
    TcPost044ErasureCodeFailure,
    TcPost045InternalScripts,
]

assert len(GROUP_B_TESTS) == 41, "GROUP_B_TESTS phai co dung 41 test case (TC-POST-001..025, 030..045)"

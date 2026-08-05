"""Epic 10 Story 10.5: Group C (compatibility) test cases from
docs/ceph-upgrade-test-cases.md, section 3.5 (`### 3.5 Tuong thich client`,
nested under the document's own "## 3. NHOM B" heading -- a documentation
quirk, not a hint these belong to Group B; confirmed by the `TC-COMPAT-*` ID
prefix and by epics.md's FR36, which lists compatibility as its own group).

**Count**: exactly 8 test cases, TC-COMPAT-001..008, no gaps -- confirmed by
grepping every `#### TC-COMPAT-` heading (Stories 10.3/10.4 both guessed
their group's count wrong before doing this).

Uses the shared `require_*`/`run_script`/`parse_steps`/`step_exit_code`
helpers `worker/executor/test_runner/framework.py` added in this same story
(Story 10.5 is the 3rd consumer of the shape group_b.py invented for Story
10.4 -- centralized rather than re-derived a 3rd time; see framework.py's
own comment on why group_a.py/group_b.py's existing private copies were
left alone).
"""

from __future__ import annotations

import re

from worker.executor.ssh_executor import execute_background
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
    check_background_handle_health,
    parse_steps,
    require_client_host,
    require_mon_host,
    run_ceph_command,
    run_script,
    step_exit_code,
)

__all__ = [
    "TcCompat001OldClientDuringUpgrade",
    "TcCompat002KernelRbdClient",
    "TcCompat003KernelCephfsClient",
    "TcCompat004CephFuseOldClient",
    "TcCompat005OpenstackIntegration",
    "TcCompat006KubernetesCephCsi",
    "TcCompat007S3Sdk",
    "TcCompat008MinCompatClient",
    "GROUP_C_TESTS",
]

RBD_POOL = "rbd_rep"
CEPHFS_ADMIN_SECRET = "/etc/ceph/admin.secret"
S3_BUCKET = "upgrade-test-bucket"


class TcCompat001OldClientDuringUpgrade(TestCase):
    """Client 14.2.22 -> cum 16.2.15. The document's own instruction is
    "tiep tuc tai o TC-RUN-010 them >=1 gio sau khi cum HEALTH_OK" -- this
    engine has no cross-test shared state (each TestCase instance is
    independent; Story 10.8's persistence layer is what will eventually let
    a later test observe an earlier one's live state), so this launches its
    OWN equivalent 3-load mix (RBD fio + CephFS I/O loop + S3 list loop,
    same shape as group_a.py's TcRun010) rather than assuming TC-RUN-010's
    process is still running. Distinct device/mount paths from TC-RUN-010's
    to avoid any collision if both happened to run concurrently.
    """

    id = "TC-COMPAT-001"
    name = "Client 14.2.22 -> cum 16.2.15"
    group = TestGroup.C
    priority = TestPriority.P1
    background = True

    RBD_DEVICE = "/dev/rbd4"
    CEPHFS_MOUNT = "/mnt/cephfs_compat"
    ERROR_KEYWORDS = (
        "input/output error",
        "no such file or directory",
        "nosuchbucket",
        "connection refused",
    )

    def start(self, ctx: TestRunContext, **kwargs):
        client = require_client_host(ctx, self.id)
        if not ctx.rgw_endpoint_vip:
            raise TestCaseError(f"{self.id}: chua cau hinh RGW endpoint VIP (Config -> Endpoint RGW)")
        script = (
            "fio --name=compat_client_rbd "
            f"--filename={self.RBD_DEVICE} --ioengine=libaio "
            "--rw=randrw --bs=4k --time_based --runtime=99999 --verify=crc32c & "
            f"while true; do echo test > {self.CEPHFS_MOUNT}/f.txt; "
            f"cat {self.CEPHFS_MOUNT}/f.txt >/dev/null; sleep 1; done & "
            f"while true; do aws --endpoint-url {ctx.rgw_endpoint_vip} "
            f"s3 ls s3://{S3_BUCKET}/ >/dev/null; sleep 2; done & "
            "wait"
        )
        handle = execute_background(client, f"bash -c '{script}'")
        return {"handle": handle, "error_seen": False}

    def poll(self, ctx: TestRunContext, state):
        health = check_background_handle_health(
            state["handle"], state.get("error_seen", False), self.ERROR_KEYWORDS
        )
        new_state = {"handle": state["handle"], "error_seen": health["error_seen"]}
        criteria = [
            CriterionResult(
                "0 loi trong suot 1 gio bo sung",
                passed=(False if health["error_seen"] else None),
                detail="phat hien loi trong output" if health["error_seen"] else "chua phat hien loi",
            ),
            CriterionResult(
                "Ghi nhan ro cac lenh CLI moi tren client khong co san (chap nhan duoc, khong phai loi)",
                passed=None,
                detail="can nguoi van hanh doi chieu thu cong",
            ),
        ]
        result = TestResult(
            test_id=self.id,
            status=TestStatus.PENDING,
            criteria=criteria,
            raw_output=health["new_stdout"] + health["new_stderr"],
        )
        return new_state, result


class TcCompat002KernelRbdClient(TestCase):
    """Kernel RBD client. The document's pass criterion mentions testing
    "cac phien ban kernel test (4.18/5.4)" -- multiple kernel versions --
    but Story 10.2's config only has ONE client_host, so this automates
    against whatever kernel client_host actually runs (captured via `uname
    -r` and included in the detail) rather than claiming multi-kernel
    coverage this engine can't provide.
    """

    id = "TC-COMPAT-002"
    name = "Kernel RBD client"
    group = TestGroup.C
    priority = TestPriority.P1

    IMAGE = "testimage1"
    DEVICE = "/dev/rbd5"
    DD_RC_RE = re.compile(r"DDRC:(-?\d+)")

    def run(self, ctx: TestRunContext) -> TestResult:
        client = require_client_host(ctx, self.id)
        # The `dd` write's own exit status is captured right after it runs
        # (DDRC:$?) rather than relying on the script's trailing EXIT:$?,
        # which only reflects the LAST command (`rbd unmap`) -- unmap can
        # still succeed even if the preceding write failed, which would
        # otherwise silently mask a real data-write failure as a PASS.
        script = f"""
echo "===STEP:full==="
uname -r
rbd map {RBD_POOL}/{self.IMAGE} 2>&1
dd if=/dev/zero of={self.DEVICE} bs=1M count=100 oflag=direct 2>&1
echo "DDRC:$?"
rbd unmap {self.DEVICE} 2>&1
echo "EXIT:$?"
"""
        output = run_script(client, script)
        steps = parse_steps(output)
        body = steps.get("full", "")
        exit_code = step_exit_code(body)
        dd_match = self.DD_RC_RE.search(body)
        dd_exit = int(dd_match.group(1)) if dd_match else None
        if exit_code is None or dd_exit is None:
            passed = None
        else:
            passed = exit_code == 0 and dd_exit == 0
        criteria = [
            CriterionResult(
                "Map/unmap, ghi du lieu thanh cong tren kernel client_host hien tai",
                passed=passed,
                detail=body,
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcCompat003KernelCephfsClient(TestCase):
    id = "TC-COMPAT-003"
    name = "Kernel CephFS client"
    group = TestGroup.C
    priority = TestPriority.P1

    MOUNT = "/mnt/kernel_cephfs"
    DD_RC_RE = re.compile(r"DDRC:(-?\d+)")

    def run(self, ctx: TestRunContext) -> TestResult:
        client = require_client_host(ctx, self.id)
        mon = require_mon_host(ctx, self.id)
        # DDRC:$? captures the write's own exit status separately from the
        # trailing EXIT:$? (which otherwise only reflects `umount`, same
        # exit-code-masking risk as TcCompat002KernelRbdClient above). The
        # "blocklist" step is now actually read into its own criterion --
        # it used to be captured into raw_output but never inspected, so a
        # client that genuinely got blocklisted could still report PASS.
        script = f"""
echo "===STEP:full==="
mkdir -p {self.MOUNT} 2>&1
mount -t ceph {mon}:/ {self.MOUNT} -o name=admin,secretfile={CEPHFS_ADMIN_SECRET} 2>&1
dd if=/dev/zero of={self.MOUNT}/test.bin bs=1M count=100 2>&1
echo "DDRC:$?"
umount {self.MOUNT} 2>&1
echo "EXIT:$?"
echo "===STEP:blocklist==="
ceph osd blocklist ls 2>&1
echo "EXIT:$?"
"""
        output = run_script(client, script)
        steps = parse_steps(output)
        full_body = steps.get("full", "")
        exit_code = step_exit_code(full_body)
        dd_match = self.DD_RC_RE.search(full_body)
        dd_exit = int(dd_match.group(1)) if dd_match else None
        mount_passed = None if (exit_code is None or dd_exit is None) else (exit_code == 0 and dd_exit == 0)
        blocklist_body = steps.get("blocklist", "")
        criteria = [
            CriterionResult(
                "Mount/umount/ghi du lieu thanh cong",
                passed=mount_passed,
                detail=full_body,
            ),
            CriterionResult(
                "Khong co client bi blocklist",
                passed=None,
                detail=(blocklist_body.strip() or "khong doc duoc ket qua blocklist")
                + " -- can nguoi van hanh doi chieu client_host co xuat hien trong danh sach khong",
            ),
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcCompat004CephFuseOldClient(TestCase):
    id = "TC-COMPAT-004"
    name = "ceph-fuse phien ban cu"
    group = TestGroup.C
    priority = TestPriority.P2

    MOUNT = "/mnt/fuse_old"

    def run(self, ctx: TestRunContext) -> TestResult:
        client = require_client_host(ctx, self.id)
        mon = require_mon_host(ctx, self.id)
        script = f"""
echo "===STEP:full==="
mkdir -p {self.MOUNT} 2>&1
ceph-fuse -m {mon} {self.MOUNT} 2>&1
echo test > {self.MOUNT}/test.txt 2>&1
cat {self.MOUNT}/test.txt 2>&1
fusermount -u {self.MOUNT} 2>&1
echo "EXIT:$?"
"""
        output = run_script(client, script)
        steps = parse_steps(output)
        exit_code = step_exit_code(steps.get("full", ""))
        criteria = [
            CriterionResult(
                "Mount/umount/I-O thanh cong",
                passed=(exit_code == 0) if exit_code is not None else None,
                detail=output,
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcCompat005OpenstackIntegration(TestCase):
    """Tich hop OpenStack. This project has no OpenStack config surface
    anywhere (no image/flavor/network id, no OpenStack credentials) -- the
    document's own commands assume a pre-existing OpenStack deployment this
    engine has no way to discover or supply, same category of gap as Story
    10.4's declined test cases (mirrors its precedent).
    """

    id = "TC-COMPAT-005"
    name = "Tich hop OpenStack"
    group = TestGroup.C
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        raise TestCaseDeclined(
            f"{self.id}: khong co cau hinh OpenStack (image/flavor/network id, credentials) trong du an "
            "nay -- can nguoi van hanh tu chay va doi chieu"
        )


class TcCompat006KubernetesCephCsi(TestCase):
    """Tich hop Kubernetes (Ceph-CSI). No Kubernetes config surface (no
    kubeconfig, no PVC/pod manifest files referenced by the document)
    exists in this project -- same reasoning as TC-COMPAT-005.
    """

    id = "TC-COMPAT-006"
    name = "Tich hop Kubernetes (Ceph-CSI)"
    group = TestGroup.C
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        raise TestCaseDeclined(
            f"{self.id}: khong co cau hinh Kubernetes (kubeconfig, PVC/pod manifest) trong du an nay -- "
            "can nguoi van hanh tu chay va doi chieu"
        )


class TcCompat007S3Sdk(TestCase):
    """S3 SDK. Runs a real boto3 script over SSH on client_host -- assumes
    boto3 is installed and AWS credentials are pre-configured there (same
    "client host is pre-configured" convention Story 10.4's aws-CLI-based
    S3 test cases already use), rather than inventing a new credential
    config field the document's own `<key>`/`<secret>` placeholders don't
    map to anything this project collects.
    """

    id = "TC-COMPAT-007"
    name = "S3 SDK"
    group = TestGroup.C
    priority = TestPriority.P1

    BUCKET = "sdk-test"

    def run(self, ctx: TestRunContext) -> TestResult:
        client = require_client_host(ctx, self.id)
        if not ctx.rgw_endpoint_vip:
            raise TestCaseError(f"{self.id}: chua cau hinh RGW endpoint VIP (Config -> Endpoint RGW)")
        py_script = f"""import boto3
s3 = boto3.client('s3', endpoint_url='{ctx.rgw_endpoint_vip}')
s3.create_bucket(Bucket='{self.BUCKET}')
s3.put_object(Bucket='{self.BUCKET}', Key='test.txt', Body=b'hello')
obj = s3.get_object(Bucket='{self.BUCKET}', Key='test.txt')
print('GET_OK' if obj['Body'].read() == b'hello' else 'GET_MISMATCH')
print(s3.list_objects_v2(Bucket='{self.BUCKET}'))
s3.delete_object(Bucket='{self.BUCKET}', Key='test.txt')
print('DONE')
"""
        script = f"""
echo "===STEP:full==="
python3 -c "{py_script}" 2>&1
echo "EXIT:$?"
"""
        output = run_script(client, script)
        steps = parse_steps(output)
        body = steps.get("full", "")
        exit_code = step_exit_code(body)
        criteria = [
            CriterionResult(
                "Toan bo thao tac co ban (put/get/list/delete) thanh cong",
                passed=(exit_code == 0 and "GET_OK" in body and "DONE" in body) if exit_code is not None else None,
                detail=body,
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcCompat008MinCompatClient(TestCase):
    id = "TC-COMPAT-008"
    name = "require-min-compat-client"
    group = TestGroup.C
    priority = TestPriority.P2

    def run(self, ctx: TestRunContext) -> TestResult:
        mon = require_mon_host(ctx, self.id)
        dump_output = run_ceph_command(mon, "ceph osd dump")
        features_output = run_ceph_command(mon, "ceph features")
        min_compat_lines = [ln for ln in dump_output.splitlines() if "min_compat_client" in ln]
        has_value = bool(min_compat_lines)
        criteria = [
            CriterionResult(
                "Gia tri hop le, khong tu choi client hop phap dang hoat dong",
                passed=None,
                detail=(
                    "; ".join(min_compat_lines)
                    if has_value
                    else "khong tim thay min_compat_client trong osd dump"
                )
                + " -- can nguoi van hanh doi chieu voi client thuc te dang ket noi",
            )
        ]
        return TestResult(
            test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=dump_output + "\n" + features_output
        )


GROUP_C_TESTS: list[type[TestCase]] = [
    TcCompat001OldClientDuringUpgrade,
    TcCompat002KernelRbdClient,
    TcCompat003KernelCephfsClient,
    TcCompat004CephFuseOldClient,
    TcCompat005OpenstackIntegration,
    TcCompat006KubernetesCephCsi,
    TcCompat007S3Sdk,
    TcCompat008MinCompatClient,
]

assert len(GROUP_C_TESTS) == 8, "GROUP_C_TESTS phai co dung 8 test case (TC-COMPAT-001..008)"

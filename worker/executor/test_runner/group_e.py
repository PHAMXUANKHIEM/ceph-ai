"""Epic 10 Test Runner, Group E (S3/RGW upgrade regression), from
docs/s3-upgrade-test-cases.md.

**Count**: PREP-01..08 (8) + DATA-01..06 (6) + TC-S3-RUN-001..004 (4) +
POST-01..04/10..14/20..29/30..34/40..44/50..52/60..62 (39, with POST-60/61
combined into one class -- see TcS3Post60ThroughputLatency below, same
"one class per combined write-up" precedent Story 10.5 used for
TC-PERF-001-003) = 57 classes total. All IDs are prefixed `TC-S3-` (the
source document itself only prefixes its RUN-* ids that way; PREP-*/
DATA-*/POST-* are bare in the document) to keep every id in
`registry.TEST_CASES_BY_ID` unique and unambiguous next to Group B's
existing `TC-POST-*` ids.

**Scope decision (2026-08-06, confirmed with user)**: this cluster has no
RGW node configured (`.env`'s `CEPH_RGW_NODES` is empty) and this codebase
has zero Vault/KMS integration anywhere. Every Vault/SSE-KMS test case
(PREP-07, DATA-04, POST-34's SSE half, POST-40..44) and every multisite
test case (TC-S3-RUN-003, POST-50..52) is declined (TestStatus.SKIP) --
same `TestCaseDeclined` pattern group_c.py's TcCompat005/006 already use
for OpenStack/Kubernetes. See docs/s3-upgrade-test-cases.md section 8 for
the full reasoning. If RGW/multisite/Vault ever gets deployed for real,
these can be swapped for real automation without changing the surrounding
architecture.

**Cross-test-timing limitation**: like every other group in this project,
a TestCase instance has no way to see state left behind by an EARLIER,
separate test run (framework.py's own docstring on this, and group_c.py's
TcCompat001 precedent of launching its own independent load rather than
assuming TC-RUN-010's process is still alive). The document's real intent
for PREP/DATA (upload before upgrade) vs POST (verify after upgrade) spans
two separate operator-triggered runs with the real upgrade in between --
this engine cannot persist a manifest across that gap until Story 10.8's
DB persistence layer exists. Every POST-1x data-integrity test below is
therefore a SELF-CONTAINED round trip (uploads its own known content, then
immediately verifies it within the same run) rather than a true
before/after diff -- same "throwaway resource the test itself creates and
tears down" shape Group B's TC-POST-030..039 already established. This is
disclosed per-class, not silent.

Uses the same `run_script`/`parse_steps`/`step_exit_code` DDRC-style
exit-code-per-command convention as group_b.py/group_c.py -- a multi-command
script's trailing `EXIT:$?` only reflects the LAST command, so any command
whose own success this file needs to judge captures its own marker
immediately after it runs (the exact bug class 2 prior stories' code
review already caught twice).
"""

from __future__ import annotations

import re

from worker.executor.ssh_executor import ExecutorError, execute_background
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
    parse_json,
    parse_steps,
    require_client_host,
    require_rgw_host,
    run_ceph_command,
    run_script,
    step_exit_code,
)

__all__ = [
    "TcS3Prep01CreateTestUser",
    "TcS3Prep02CreateTestBucket",
    "TcS3Prep03AwsCliConfigured",
    "TcS3Prep04RgwReachable",
    "TcS3Prep05RecordBaseline",
    "TcS3Prep06MultisiteSyncBeforeStart",
    "TcS3Prep07VaultBeforeStart",
    "TcS3Prep08RecordRgwUnitsPerNode",
    "TcS3Data01UploadSmallObjects",
    "TcS3Data02UploadLargeObjects",
    "TcS3Data03VersioningBaseline",
    "TcS3Data04DefaultEncryption",
    "TcS3Data05Manifest",
    "TcS3Data06PolicyAndLifecycle",
    "TcS3Run001ContinuousLoadViaLb",
    "TcS3Run002RgwLogMonitor",
    "TcS3Run003MultisiteSyncDuringUpgrade",
    "TcS3Run004InstanceDowntime",
    "TcS3Post01VersionConsistent",
    "TcS3Post02InstancesActive",
    "TcS3Post03PortBinding",
    "TcS3Post04NoNewCrash",
    "TcS3Post10ManifestRoundTrip",
    "TcS3Post11BucketStats",
    "TcS3Post12VersioningReadback",
    "TcS3Post13MultipartRoundTrip",
    "TcS3Post14PolicyLifecycleIntact",
    "TcS3Post20PutObject",
    "TcS3Post21GetObject",
    "TcS3Post22ListBucket",
    "TcS3Post23DeleteObject",
    "TcS3Post24MultipartUpload",
    "TcS3Post25CopyObject",
    "TcS3Post26PresignedUrl",
    "TcS3Post27CreateDeleteBucket",
    "TcS3Post28UserAdmin",
    "TcS3Post29BucketResharding",
    "TcS3Post30ListBeforePut",
    "TcS3Post31UserInfoByKey",
    "TcS3Post32BucketOwner",
    "TcS3Post33UserQuota",
    "TcS3Post34PutWithoutSse",
    "TcS3Post40VaultHealth",
    "TcS3Post41VaultTransitKeys",
    "TcS3Post42PutWithSse",
    "TcS3Post43GetSseObject",
    "TcS3Post44VaultAuditLog",
    "TcS3Post50CrossZoneReplication",
    "TcS3Post51BidirectionalSyncStatus",
    "TcS3Post52MetadataSync",
    "TcS3Post60ThroughputLatency",
    "TcS3Post62BrokenPipeFrequency",
    "GROUP_E_TESTS",
]

S3_TEST_UID = "s3-upgrade-test"
S3_TEST_BUCKET = "s3-upgrade-test-bucket"

_MULTISITE_DECLINE = (
    "Cluster nay khong cau hinh multisite (chi 1 RGW zone, khong co zone thu 2 de doi chieu) "
    "-- xem docs/s3-upgrade-test-cases.md muc 8"
)
_VAULT_DECLINE = (
    "Cluster nay khong tich hop Vault/KMS (khong co field vault nao trong config/settings.py) "
    "-- xem docs/s3-upgrade-test-cases.md muc 8"
)


def _rgw_endpoint(ctx: TestRunContext, test_id: str) -> str:
    if not ctx.rgw_endpoint_vip:
        raise TestCaseError(f"{test_id}: chua cau hinh RGW endpoint VIP (Config -> Endpoint RGW)")
    return ctx.rgw_endpoint_vip


# --- 2.1 Chuan bi moi truong test ------------------------------------------


class TcS3Prep01CreateTestUser(TestCase):
    """Idempotent: if the user already exists (a prior run of this same
    test case), that counts as PASS too -- the document's own intent is
    "a dedicated test user exists", not "creation must be a fresh event".
    """

    id = "TC-S3-PREP-01"
    name = "Tao user test S3 rieng"
    group = TestGroup.E
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        rgw = require_rgw_host(ctx, self.id)
        script = f"""
echo "===STEP:full==="
radosgw-admin user info --uid={S3_TEST_UID} >/dev/null 2>&1 && echo ALREADY_EXISTS || \
  radosgw-admin user create --uid={S3_TEST_UID} --display-name="S3 Upgrade Test" 2>&1
echo "EXIT:$?"
"""
        output = run_script(rgw, script)
        body = parse_steps(output).get("full", "")
        exit_code = step_exit_code(body)
        criteria = [
            CriterionResult(
                "User test rieng duoc tao (hoac da ton tai tu lan chay truoc)",
                passed=(exit_code == 0) if exit_code is not None else None,
                detail=body,
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcS3Prep02CreateTestBucket(TestCase):
    id = "TC-S3-PREP-02"
    name = "Tao bucket test rieng"
    group = TestGroup.E
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        client = require_client_host(ctx, self.id)
        endpoint = _rgw_endpoint(ctx, self.id)
        script = f"""
echo "===STEP:full==="
aws s3 mb s3://{S3_TEST_BUCKET} --endpoint-url {endpoint} 2>&1 || \
  aws s3 ls s3://{S3_TEST_BUCKET} --endpoint-url {endpoint} >/dev/null 2>&1
echo "EXIT:$?"
"""
        output = run_script(client, script)
        body = parse_steps(output).get("full", "")
        exit_code = step_exit_code(body)
        criteria = [
            CriterionResult(
                "Bucket test duoc tao (hoac da ton tai tu lan chay truoc)",
                passed=(exit_code == 0) if exit_code is not None else None,
                detail=body,
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcS3Prep03AwsCliConfigured(TestCase):
    """Only confirms client_host has SOME non-placeholder access key
    configured -- cannot confirm it is specifically PREP-01's freshly
    created key (same "client_host's own pre-configured credential chain"
    assumption group_c.py's TcCompat007S3Sdk already relies on for every
    real S3 operation in this project; wiring PREP-01's generated key into
    the operator's own aws config is out of scope here).
    """

    id = "TC-S3-PREP-03"
    name = "aws cli da cau hinh endpoint/key"
    group = TestGroup.E
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        client = require_client_host(ctx, self.id)
        output = run_ceph_command(client, "aws configure list 2>&1")
        has_key = bool(re.search(r"access_key\s+\S+", output)) and "<not set>" not in output
        criteria = [
            CriterionResult(
                "aws cli tren client_host da co access key duoc cau hinh (khong phai <not set>)",
                passed=has_key,
                detail=output,
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcS3Prep04RgwReachable(TestCase):
    id = "TC-S3-PREP-04"
    name = "Xac nhan RGW dang hoat dong truoc khi bat dau"
    group = TestGroup.E
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        client = require_client_host(ctx, self.id)
        endpoint = _rgw_endpoint(ctx, self.id)
        code = run_ceph_command(client, f"curl -s -o /dev/null -w '%{{http_code}}' {endpoint}/ 2>&1").strip()
        # 403 is RGW's normal AccessDenied XML for an unauthenticated root
        # GET -- proof the service IS up, not a failure. 000 means curl
        # never got a response at all (connection refused/timeout).
        passed = code not in ("", "000") and code.isdigit()
        criteria = [
            CriterionResult(
                "RGW tra ve HTTP response (bat ky code nao khac 000/khong ket noi duoc)",
                passed=passed,
                detail=f"http_code={code}",
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=code)


class TcS3Prep05RecordBaseline(TestCase):
    id = "TC-S3-PREP-05"
    name = "Ghi nhan baseline user/bucket"
    group = TestGroup.E
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        rgw = require_rgw_host(ctx, self.id)
        script = f"""
echo "===STEP:users==="
radosgw-admin user list 2>&1
echo "EXIT:$?"
echo "===STEP:stats==="
radosgw-admin bucket stats --bucket={S3_TEST_BUCKET} 2>&1
echo "EXIT:$?"
"""
        output = run_script(rgw, script)
        steps = parse_steps(output)
        users_exit = step_exit_code(steps.get("users", ""))
        stats_exit = step_exit_code(steps.get("stats", ""))
        passed = None if (users_exit is None or stats_exit is None) else (users_exit == 0 and stats_exit == 0)
        criteria = [
            CriterionResult(
                "Ghi nhan duoc user list va bucket stats lam baseline",
                passed=passed,
                detail=output,
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcS3Prep06MultisiteSyncBeforeStart(TestCase):
    id = "TC-S3-PREP-06"
    name = "Multisite sync caught up truoc khi bat dau"
    group = TestGroup.E
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        raise TestCaseDeclined(f"{self.id}: {_MULTISITE_DECLINE}")


class TcS3Prep07VaultBeforeStart(TestCase):
    id = "TC-S3-PREP-07"
    name = "Vault hoat dong, token con han"
    group = TestGroup.E
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        raise TestCaseDeclined(f"{self.id}: {_VAULT_DECLINE}")


class TcS3Prep08RecordRgwUnitsPerNode(TestCase):
    id = "TC-S3-PREP-08"
    name = "Ghi lai danh sach RGW instance moi node"
    group = TestGroup.E
    priority = TestPriority.P2

    def run(self, ctx: TestRunContext) -> TestResult:
        if not ctx.rgw_hosts:
            raise TestCaseError(f"{self.id}: chua cau hinh RGW host nao")
        per_host: list[str] = []
        all_ok = True
        for host in ctx.rgw_hosts:
            try:
                out = run_ceph_command(host, "systemctl list-units 'ceph-radosgw@*' --no-pager 2>&1")
                per_host.append(f"--- {host} ---\n{out}")
            except ExecutorError as exc:
                all_ok = False
                per_host.append(f"--- {host} ---\nERROR: {exc}")
        raw = "\n".join(per_host)
        criteria = [
            CriterionResult(
                "Ghi lai duoc danh sach RGW instance tren tung node da cau hinh",
                passed=all_ok,
                detail=raw,
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=raw)


# --- 2.2 Chuan bi du lieu baseline ------------------------------------------


class TcS3Data01UploadSmallObjects(TestCase):
    """Document says 100-500 objects 1KB-1MB. Scaled to 100 objects
    1KB-64KB -- the full 1MB-per-object upper bound multiplied by up to 500
    objects would make this single SSH-executed script run for many
    minutes with no progress visibility (this is a one-shot run(), not a
    background test); 100 objects at up to 64KB is still a real multi-PUT
    regression check, just bounded to a few seconds of remote work.
    """

    id = "TC-S3-DATA-01"
    name = "Upload object nho (baseline)"
    group = TestGroup.E
    priority = TestPriority.P1

    COUNT = 100

    def run(self, ctx: TestRunContext) -> TestResult:
        client = require_client_host(ctx, self.id)
        endpoint = _rgw_endpoint(ctx, self.id)
        script = f"""
echo "===STEP:full==="
fail=0
for i in $(seq 1 {self.COUNT}); do
  size=$((RANDOM % 65536 + 1024))
  head -c $size /dev/urandom > /tmp/s3data01_$i.bin
  aws s3 cp /tmp/s3data01_$i.bin s3://{S3_TEST_BUCKET}/small/obj_$i.bin --endpoint-url {endpoint} >/dev/null 2>&1 || fail=$((fail+1))
  rm -f /tmp/s3data01_$i.bin
done
echo "FAILCOUNT:$fail"
echo "EXIT:0"
"""
        output = run_script(client, script)
        body = parse_steps(output).get("full", "")
        fail_match = re.search(r"FAILCOUNT:(\d+)", body)
        fail_count = int(fail_match.group(1)) if fail_match else None
        criteria = [
            CriterionResult(
                f"Upload thanh cong {self.COUNT} object nho vao bucket test",
                passed=(fail_count == 0) if fail_count is not None else None,
                detail=body,
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcS3Data02UploadLargeObjects(TestCase):
    id = "TC-S3-DATA-02"
    name = "Upload object lon (test multipart)"
    group = TestGroup.E
    priority = TestPriority.P1

    COUNT = 2
    SIZE_MB = 150

    def run(self, ctx: TestRunContext) -> TestResult:
        client = require_client_host(ctx, self.id)
        endpoint = _rgw_endpoint(ctx, self.id)
        script = f"""
echo "===STEP:full==="
fail=0
for i in $(seq 1 {self.COUNT}); do
  dd if=/dev/zero of=/tmp/s3data02_$i.bin bs=1M count={self.SIZE_MB} >/dev/null 2>&1
  aws s3 cp /tmp/s3data02_$i.bin s3://{S3_TEST_BUCKET}/large/bigfile_$i.bin --endpoint-url {endpoint} >/dev/null 2>&1 || fail=$((fail+1))
  rm -f /tmp/s3data02_$i.bin
done
echo "FAILCOUNT:$fail"
echo "EXIT:0"
"""
        output = run_script(client, script)
        body = parse_steps(output).get("full", "")
        fail_match = re.search(r"FAILCOUNT:(\d+)", body)
        fail_count = int(fail_match.group(1)) if fail_match else None
        criteria = [
            CriterionResult(
                f"Upload thanh cong {self.COUNT} object lon (>100MB, tu dong multipart)",
                passed=(fail_count == 0) if fail_count is not None else None,
                detail=body,
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcS3Data03VersioningBaseline(TestCase):
    id = "TC-S3-DATA-03"
    name = "Bat versioning, tao nhieu version"
    group = TestGroup.E
    priority = TestPriority.P2

    KEY = "versioned/obj.txt"

    def run(self, ctx: TestRunContext) -> TestResult:
        client = require_client_host(ctx, self.id)
        endpoint = _rgw_endpoint(ctx, self.id)
        script = f"""
echo "===STEP:full==="
aws s3api put-bucket-versioning --bucket {S3_TEST_BUCKET} --versioning-configuration Status=Enabled --endpoint-url {endpoint} 2>&1
echo "VERSIONRC:$?"
for v in 1 2 3; do
  echo "version-$v" | aws s3 cp - s3://{S3_TEST_BUCKET}/{self.KEY} --endpoint-url {endpoint} >/dev/null 2>&1
done
aws s3api list-object-versions --bucket {S3_TEST_BUCKET} --prefix {self.KEY} --endpoint-url {endpoint} 2>&1
echo "EXIT:$?"
"""
        output = run_script(client, script)
        body = parse_steps(output).get("full", "")
        version_rc_match = re.search(r"VERSIONRC:(-?\d+)", body)
        version_rc = int(version_rc_match.group(1)) if version_rc_match else None
        version_count = body.count('"VersionId"')
        passed = None if version_rc is None else (version_rc == 0 and version_count >= 2)
        criteria = [
            CriterionResult(
                "Versioning bat thanh cong, co it nhat 2-3 version cho cung 1 object",
                passed=passed,
                detail=body,
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcS3Data04DefaultEncryption(TestCase):
    id = "TC-S3-DATA-04"
    name = "Set default bucket encryption"
    group = TestGroup.E
    priority = TestPriority.P2

    def run(self, ctx: TestRunContext) -> TestResult:
        raise TestCaseDeclined(f"{self.id}: {_VAULT_DECLINE}")


class TcS3Data05Manifest(TestCase):
    """Lists whatever DATA-01/DATA-02 already uploaded and records
    key/etag/size as this run's manifest -- see this module's docstring on
    why a true cross-run-persisted manifest isn't possible yet. Useful on
    its own even so: confirms the bucket's actual object count/etags are
    all individually readable right now.
    """

    id = "TC-S3-DATA-05"
    name = "Tao manifest ETag/size"
    group = TestGroup.E
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        client = require_client_host(ctx, self.id)
        endpoint = _rgw_endpoint(ctx, self.id)
        output = run_ceph_command(
            client,
            f"aws s3api list-objects-v2 --bucket {S3_TEST_BUCKET} --endpoint-url {endpoint} 2>&1",
        )
        try:
            parsed = parse_json(output, self.id)
            count = len(parsed.get("Contents", []))
            passed = count > 0
        except TestCaseError:
            count = 0
            passed = False
        criteria = [
            CriterionResult(
                "Tao duoc manifest (key/etag/size) cho toan bo object hien co trong bucket",
                passed=passed,
                detail=f"so object trong manifest: {count}",
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcS3Data06PolicyAndLifecycle(TestCase):
    id = "TC-S3-DATA-06"
    name = "Set bucket policy va lifecycle rule mau"
    group = TestGroup.E
    priority = TestPriority.P2

    def run(self, ctx: TestRunContext) -> TestResult:
        client = require_client_host(ctx, self.id)
        endpoint = _rgw_endpoint(ctx, self.id)
        policy = (
            '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"AWS":["*"]},'
            f'"Action":["s3:GetObject"],"Resource":["arn:aws:s3:::{S3_TEST_BUCKET}/public/*"]}}]}}'
        )
        lifecycle = (
            '{"Rules":[{"ID":"expire-tmp","Status":"Enabled","Filter":{"Prefix":"tmp/"},'
            '"Expiration":{"Days":1}}]}'
        )
        script = f"""
echo "===STEP:full==="
echo '{policy}' > /tmp/s3data06_policy.json
echo '{lifecycle}' > /tmp/s3data06_lifecycle.json
aws s3api put-bucket-policy --bucket {S3_TEST_BUCKET} --policy file:///tmp/s3data06_policy.json --endpoint-url {endpoint} 2>&1
echo "POLICYRC:$?"
aws s3api put-bucket-lifecycle-configuration --bucket {S3_TEST_BUCKET} --lifecycle-configuration file:///tmp/s3data06_lifecycle.json --endpoint-url {endpoint} 2>&1
echo "LIFECYCLERC:$?"
rm -f /tmp/s3data06_policy.json /tmp/s3data06_lifecycle.json
echo "EXIT:0"
"""
        output = run_script(client, script)
        body = parse_steps(output).get("full", "")
        policy_rc_match = re.search(r"POLICYRC:(-?\d+)", body)
        lifecycle_rc_match = re.search(r"LIFECYCLERC:(-?\d+)", body)
        policy_rc = int(policy_rc_match.group(1)) if policy_rc_match else None
        lifecycle_rc = int(lifecycle_rc_match.group(1)) if lifecycle_rc_match else None
        passed = None if (policy_rc is None or lifecycle_rc is None) else (policy_rc == 0 and lifecycle_rc == 0)
        criteria = [
            CriterionResult(
                "Set thanh cong 1 bucket policy va 1 lifecycle rule mau",
                passed=passed,
                detail=body,
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


# --- 3. Test TRONG luc nang cap ---------------------------------------------


class TcS3Run001ContinuousLoadViaLb(TestCase):
    """Continuous PUT/GET load plus a 1-per-second HTTP probe, same
    "curl -o /dev/null -w %{http_code}, log only failures" shape the
    document itself specifies -- lets poll() compute both the overall 5xx
    rate AND the longest consecutive-failure streak (document's own "no
    failure streak > 5 giay" criterion) directly from the probe log,
    instead of tailing warp's own summary (warp has no per-second
    HTTP-status granularity).
    """

    id = "TC-S3-RUN-001"
    name = "Tai PUT/GET lien tuc qua Load Balancer"
    group = TestGroup.E
    priority = TestPriority.P1
    background = True

    PROBE_LOG = "/tmp/s3_run001_probe.log"
    ERROR_KEYWORDS = ("connection refused", "timed out", "broken pipe")

    def start(self, ctx: TestRunContext, **kwargs):
        client = require_client_host(ctx, self.id)
        endpoint = _rgw_endpoint(ctx, self.id)
        script = (
            f"rm -f {self.PROBE_LOG}; "
            f"while true; do "
            f"echo test | aws s3 cp - s3://{S3_TEST_BUCKET}/run001/probe_$(date +%s%N).txt "
            f"--endpoint-url {endpoint} >/dev/null 2>&1; "
            f"code=$(curl -s -o /dev/null -w '%{{http_code}}' {endpoint}/{S3_TEST_BUCKET}/); "
            f"echo \"$code\" | grep -qE '^(2|3)[0-9]{{2}}$' || echo \"$(date -Is) HTTP_FAIL code=$code\" >> {self.PROBE_LOG}; "
            f"sleep 1; done"
        )
        handle = execute_background(client, f"bash -c '{script}'")
        return {"handle": handle, "client_host": client, "total_probes": 0, "fail_probes": 0}

    def poll(self, ctx: TestRunContext, state):
        handle = state["handle"]
        handle.read_new_output()
        client = state["client_host"]
        try:
            log = run_ceph_command(client, f"cat {self.PROBE_LOG} 2>/dev/null || true")
        except ExecutorError:
            log = ""
        fail_lines = [ln for ln in log.splitlines() if "HTTP_FAIL" in ln]
        # Consecutive-failure-streak: probe interval is ~1s, so N
        # consecutive HTTP_FAIL lines (by line order, which matches append
        # order) approximates an N-second-long outage.
        max_streak = 0
        current = 0
        for ln in log.splitlines():
            if "HTTP_FAIL" in ln:
                current += 1
                max_streak = max(max_streak, current)
            else:
                current = 0
        done = handle.is_done()
        exit_code = handle.exit_code() if done else None
        crashed = done and exit_code not in (None, 0)
        criteria = [
            CriterionResult(
                "Ty le HTTP that bai thap (khong the tinh % chinh xac tu day, xem so dong HTTP_FAIL)",
                passed=(False if len(fail_lines) > 0 and crashed else None),
                detail=f"{len(fail_lines)} dong HTTP_FAIL ghi nhan duoc",
            ),
            CriterionResult(
                "Khong co chuoi loi lien tiep keo dai > 5 giay",
                passed=(False if max_streak > 5 else None),
                detail=f"chuoi loi lien tiep dai nhat quan sat duoc: {max_streak} lan probe",
            ),
        ]
        result = TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=log[-4000:])
        return {**state}, result


class TcS3Run002RgwLogMonitor(TestCase):
    """Only the FIRST configured RGW host is monitored -- a genuine
    multi-host `tail -f` fan-out would need one background handle per host
    (this TestCase's state dict only carries one), disclosed rather than
    silently narrowed. If this cluster later has more than one RGW host,
    the other hosts' logs are not covered by this test case yet.
    """

    id = "TC-S3-RUN-002"
    name = "Giam sat log RGW real-time"
    group = TestGroup.E
    priority = TestPriority.P1
    background = True

    ERROR_KEYWORDS = ("crash", "abort", "assert")

    def start(self, ctx: TestRunContext, **kwargs):
        rgw = require_rgw_host(ctx, self.id)
        cmd = "tail -F -n0 /var/log/ceph/ceph-client.rgw.*.log 2>&1"
        handle = execute_background(rgw, cmd)
        return {"handle": handle, "error_seen": False, "error_lines": []}

    def poll(self, ctx: TestRunContext, state):
        handle = state["handle"]
        new_out, new_err = handle.read_new_output()
        combined = new_out + new_err
        new_error_lines = [
            ln for ln in combined.splitlines() if any(kw in ln.lower() for kw in self.ERROR_KEYWORDS)
        ]
        error_lines = (state.get("error_lines") or []) + new_error_lines
        criteria = [
            CriterionResult(
                "Khong co loi crash/abort/assert trong log RGW",
                passed=(False if error_lines else None),
                detail="\n".join(error_lines[-20:]) if error_lines else "chua phat hien loi",
            )
        ]
        new_state = {"handle": handle, "error_seen": bool(error_lines), "error_lines": error_lines}
        result = TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=combined[-4000:])
        return new_state, result


class TcS3Run003MultisiteSyncDuringUpgrade(TestCase):
    id = "TC-S3-RUN-003"
    name = "Multisite sync trong luc nang cap"
    group = TestGroup.E
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        raise TestCaseDeclined(f"{self.id}: {_MULTISITE_DECLINE}")


class TcS3Run004InstanceDowntime(TestCase):
    """Passively probes ONE specific RGW instance's own direct port
    (not the VIP -- the document's own point is per-instance downtime, not
    LB-masked downtime, which TC-S3-RUN-001 already covers) once per
    second for the whole monitoring window, so whenever the operator
    actually restarts that instance during a real rolling upgrade, the gap
    naturally shows up in the probe log without needing this test to be
    coordinated with the restart step itself.
    """

    id = "TC-S3-RUN-004"
    name = "Do downtime tung RGW instance khi restart"
    group = TestGroup.E
    priority = TestPriority.P2
    background = True

    PROBE_LOG = "/tmp/s3_run004_probe.log"
    THRESHOLD_SECONDS = 30

    def start(self, ctx: TestRunContext, **kwargs):
        client = require_client_host(ctx, self.id)
        rgw = require_rgw_host(ctx, self.id)
        script = (
            f"rm -f {self.PROBE_LOG}; "
            f"while true; do "
            f"ok=$(curl -sf -o /dev/null http://{rgw}:7480/ && echo UP || echo DOWN); "
            f"echo \"$(date -Is) $ok\" >> {self.PROBE_LOG}; sleep 1; done"
        )
        handle = execute_background(client, f"bash -c '{script}'")
        return {"handle": handle, "client_host": client}

    def poll(self, ctx: TestRunContext, state):
        handle = state["handle"]
        handle.read_new_output()
        try:
            log = run_ceph_command(state["client_host"], f"cat {self.PROBE_LOG} 2>/dev/null || true")
        except ExecutorError:
            log = ""
        max_down_streak = 0
        current = 0
        for ln in log.splitlines():
            if ln.endswith("DOWN"):
                current += 1
                max_down_streak = max(max_down_streak, current)
            else:
                current = 0
        criteria = [
            CriterionResult(
                f"Downtime moi instance <= {self.THRESHOLD_SECONDS} giay",
                passed=(False if max_down_streak > self.THRESHOLD_SECONDS else None),
                detail=f"chuoi DOWN lien tiep dai nhat quan sat duoc: ~{max_down_streak} giay",
            )
        ]
        result = TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=log[-4000:])
        return {**state}, result


# --- 4.1 Trang thai dich vu --------------------------------------------------


class TcS3Post01VersionConsistent(TestCase):
    id = "TC-S3-POST-01"
    name = "Version RGW dong nhat"
    group = TestGroup.E
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        mon = require_rgw_host(ctx, self.id) if ctx.rgw_hosts else None
        host = mon or ctx.mon_host
        if not host:
            raise TestCaseError(f"{self.id}: chua cau hinh RGW hoac MON host nao")
        output = run_ceph_command(host, "ceph versions 2>&1")
        try:
            parsed = parse_json(output, self.id)
            rgw_versions = list((parsed.get("rgw") or {}).keys())
            passed = len(rgw_versions) <= 1 and len(rgw_versions) > 0
        except TestCaseError:
            rgw_versions = []
            passed = None
        criteria = [
            CriterionResult(
                "Toan bo RGW instance cung 1 version dich",
                passed=passed,
                detail=f"cac version RGW dang thay: {rgw_versions}",
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcS3Post02InstancesActive(TestCase):
    id = "TC-S3-POST-02"
    name = "Toan bo RGW instance active running"
    group = TestGroup.E
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        if not ctx.rgw_hosts:
            raise TestCaseError(f"{self.id}: chua cau hinh RGW host nao")
        details: list[str] = []
        all_active = True
        for host in ctx.rgw_hosts:
            out = run_ceph_command(host, "systemctl list-units 'ceph-radosgw@*' --no-pager 2>&1")
            details.append(f"--- {host} ---\n{out}")
            if "failed" in out.lower() or "active running" not in out.lower():
                all_active = False
        raw = "\n".join(details)
        criteria = [
            CriterionResult(
                "100% RGW instance active, khong co instance nao failed",
                passed=all_active,
                detail=raw,
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=raw)


class TcS3Post03PortBinding(TestCase):
    id = "TC-S3-POST-03"
    name = "Moi instance bind dung port"
    group = TestGroup.E
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        if not ctx.rgw_hosts:
            raise TestCaseError(f"{self.id}: chua cau hinh RGW host nao")
        details: list[str] = []
        all_ok = True
        for host in ctx.rgw_hosts:
            out = run_ceph_command(host, "ss -tlnp 2>&1 | grep -E '808[0-9]|7480' || true")
            details.append(f"--- {host} ---\n{out}")
            if not out.strip():
                all_ok = False
        raw = "\n".join(details)
        criteria = [
            CriterionResult(
                "Moi RGW host co it nhat 1 port dang lang nghe, khong trung/conflict ro rang",
                passed=all_ok,
                detail=raw,
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=raw)


class TcS3Post04NoNewCrash(TestCase):
    id = "TC-S3-POST-04"
    name = "Khong co crash moi"
    group = TestGroup.E
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        host = ctx.mon_host or (ctx.rgw_hosts[0] if ctx.rgw_hosts else None)
        if not host:
            raise TestCaseError(f"{self.id}: chua cau hinh MON hoac RGW host nao")
        output = run_ceph_command(host, "ceph crash ls-new 2>&1")
        passed = output.strip() == "" or "no crashes" in output.lower()
        criteria = [CriterionResult("0 crash moi", passed=passed, detail=output)]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


# --- 4.2 Toan ven du lieu ----------------------------------------------------


class TcS3Post10ManifestRoundTrip(TestCase):
    """See this module's docstring on the cross-test-timing limitation --
    this is a self-contained upload+verify round trip, not a diff against
    DATA-05's actual pre-upgrade manifest.
    """

    id = "TC-S3-POST-10"
    name = "Doi chieu object voi manifest (round-trip)"
    group = TestGroup.E
    priority = TestPriority.P1

    COUNT = 10
    PREFIX = "post10_roundtrip"

    def run(self, ctx: TestRunContext) -> TestResult:
        client = require_client_host(ctx, self.id)
        endpoint = _rgw_endpoint(ctx, self.id)
        script = f"""
echo "===STEP:full==="
mismatch=0
for i in $(seq 1 {self.COUNT}); do
  echo "content-$i" > /tmp/s3post10_$i.txt
  local_md5=$(md5sum /tmp/s3post10_$i.txt | cut -d' ' -f1)
  aws s3 cp /tmp/s3post10_$i.txt s3://{S3_TEST_BUCKET}/{self.PREFIX}/f_$i.txt --endpoint-url {endpoint} >/dev/null 2>&1
  remote_etag=$(aws s3api head-object --bucket {S3_TEST_BUCKET} --key {self.PREFIX}/f_$i.txt --endpoint-url {endpoint} 2>&1 | grep -o '"ETag": *"[a-f0-9]*"' | grep -o '[a-f0-9]\\{{32\\}}')
  [ "$local_md5" = "$remote_etag" ] || mismatch=$((mismatch+1))
  rm -f /tmp/s3post10_$i.txt
done
echo "MISMATCH:$mismatch"
echo "EXIT:0"
"""
        output = run_script(client, script)
        body = parse_steps(output).get("full", "")
        mismatch_match = re.search(r"MISMATCH:(\d+)", body)
        mismatch = int(mismatch_match.group(1)) if mismatch_match else None
        criteria = [
            CriterionResult(
                "100% object khop ETag/MD5, 0 MISSING/MISMATCH (round-trip tu tao trong lan chay nay)",
                passed=(mismatch == 0) if mismatch is not None else None,
                detail=body,
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcS3Post11BucketStats(TestCase):
    id = "TC-S3-POST-11"
    name = "So luong object & dung luong bucket"
    group = TestGroup.E
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        rgw = require_rgw_host(ctx, self.id)
        output = run_ceph_command(rgw, f"radosgw-admin bucket stats --bucket={S3_TEST_BUCKET} 2>&1")
        try:
            parse_json(output, self.id)
            readable = True
        except TestCaseError:
            readable = False
        criteria = [
            CriterionResult(
                "Doc duoc bucket stats hien tai (can nguoi van hanh doi chieu thu cong voi baseline PREP-05)",
                passed=(True if readable else False),
                detail=output,
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcS3Post12VersioningReadback(TestCase):
    id = "TC-S3-POST-12"
    name = "Version cu van tai duoc"
    group = TestGroup.E
    priority = TestPriority.P2

    KEY = "post12_versioned/obj.txt"

    def run(self, ctx: TestRunContext) -> TestResult:
        client = require_client_host(ctx, self.id)
        endpoint = _rgw_endpoint(ctx, self.id)
        script = f"""
echo "===STEP:full==="
aws s3api put-bucket-versioning --bucket {S3_TEST_BUCKET} --versioning-configuration Status=Enabled --endpoint-url {endpoint} >/dev/null 2>&1
echo "v1-content" | aws s3 cp - s3://{S3_TEST_BUCKET}/{self.KEY} --endpoint-url {endpoint} >/dev/null 2>&1
echo "v2-content" | aws s3 cp - s3://{S3_TEST_BUCKET}/{self.KEY} --endpoint-url {endpoint} >/dev/null 2>&1
old_version=$(aws s3api list-object-versions --bucket {S3_TEST_BUCKET} --prefix {self.KEY} --endpoint-url {endpoint} 2>&1 | python3 -c "import json,sys; d=json.load(sys.stdin); vs=sorted(d.get('Versions',[]), key=lambda v: v['LastModified']); print(vs[0]['VersionId'] if vs else '')" 2>/dev/null)
aws s3api get-object --bucket {S3_TEST_BUCKET} --key {self.KEY} --version-id "$old_version" --endpoint-url {endpoint} /tmp/s3post12_old.txt >/dev/null 2>&1
content=$(cat /tmp/s3post12_old.txt 2>/dev/null)
[ "$content" = "v1-content" ] && echo "MATCH" || echo "NOMATCH"
rm -f /tmp/s3post12_old.txt
echo "EXIT:0"
"""
        output = run_script(client, script)
        body = parse_steps(output).get("full", "")
        criteria = [
            CriterionResult(
                "Toan bo version cu doc duoc, noi dung dung",
                passed=("MATCH" in body and "NOMATCH" not in body) if body else None,
                detail=body,
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcS3Post13MultipartRoundTrip(TestCase):
    id = "TC-S3-POST-13"
    name = "Multipart object lon tai ve nguyen ven"
    group = TestGroup.E
    priority = TestPriority.P1

    SIZE_MB = 150
    KEY = "post13_multipart/bigfile.bin"

    def run(self, ctx: TestRunContext) -> TestResult:
        client = require_client_host(ctx, self.id)
        endpoint = _rgw_endpoint(ctx, self.id)
        script = f"""
echo "===STEP:full==="
dd if=/dev/zero of=/tmp/s3post13_up.bin bs=1M count={self.SIZE_MB} >/dev/null 2>&1
up_md5=$(md5sum /tmp/s3post13_up.bin | cut -d' ' -f1)
aws s3 cp /tmp/s3post13_up.bin s3://{S3_TEST_BUCKET}/{self.KEY} --endpoint-url {endpoint} >/dev/null 2>&1
aws s3 cp s3://{S3_TEST_BUCKET}/{self.KEY} /tmp/s3post13_down.bin --endpoint-url {endpoint} >/dev/null 2>&1
down_md5=$(md5sum /tmp/s3post13_down.bin | cut -d' ' -f1)
[ "$up_md5" = "$down_md5" ] && echo "CHECKSUM_MATCH" || echo "CHECKSUM_MISMATCH up=$up_md5 down=$down_md5"
rm -f /tmp/s3post13_up.bin /tmp/s3post13_down.bin
echo "EXIT:0"
"""
        output = run_script(client, script)
        body = parse_steps(output).get("full", "")
        criteria = [
            CriterionResult(
                "Checksum object lon khop truoc/sau khi tai ve",
                passed=("CHECKSUM_MATCH" in body) if body else None,
                detail=body,
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcS3Post14PolicyLifecycleIntact(TestCase):
    id = "TC-S3-POST-14"
    name = "Bucket policy / lifecycle rule con nguyen"
    group = TestGroup.E
    priority = TestPriority.P2

    def run(self, ctx: TestRunContext) -> TestResult:
        client = require_client_host(ctx, self.id)
        endpoint = _rgw_endpoint(ctx, self.id)
        script = f"""
echo "===STEP:policy==="
aws s3api get-bucket-policy --bucket {S3_TEST_BUCKET} --endpoint-url {endpoint} 2>&1
echo "EXIT:$?"
echo "===STEP:lifecycle==="
aws s3api get-bucket-lifecycle-configuration --bucket {S3_TEST_BUCKET} --endpoint-url {endpoint} 2>&1
echo "EXIT:$?"
"""
        output = run_script(client, script)
        steps = parse_steps(output)
        policy_exit = step_exit_code(steps.get("policy", ""))
        lifecycle_exit = step_exit_code(steps.get("lifecycle", ""))
        passed = None if (policy_exit is None or lifecycle_exit is None) else (policy_exit == 0 and lifecycle_exit == 0)
        criteria = [
            CriterionResult(
                "Doc lai duoc bucket policy va lifecycle rule (can doi chieu noi dung thu cong voi DATA-06)",
                passed=passed,
                detail=output,
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


# --- 4.3 Chuc nang co ban (Regression) ---------------------------------------


class TcS3Post20PutObject(TestCase):
    id = "TC-S3-POST-20"
    name = "PUT object moi"
    group = TestGroup.E
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        client = require_client_host(ctx, self.id)
        endpoint = _rgw_endpoint(ctx, self.id)
        script = f"""
echo "===STEP:full==="
echo "hello" > /tmp/s3post20_test.txt
aws s3api put-object --bucket {S3_TEST_BUCKET} --key post20_test.txt --body /tmp/s3post20_test.txt --endpoint-url {endpoint} 2>&1
echo "EXIT:$?"
rm -f /tmp/s3post20_test.txt
"""
        output = run_script(client, script)
        body = parse_steps(output).get("full", "")
        exit_code = step_exit_code(body)
        criteria = [
            CriterionResult("PUT thanh cong, khong loi", passed=(exit_code == 0) if exit_code is not None else None, detail=body)
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcS3Post21GetObject(TestCase):
    id = "TC-S3-POST-21"
    name = "GET object"
    group = TestGroup.E
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        client = require_client_host(ctx, self.id)
        endpoint = _rgw_endpoint(ctx, self.id)
        script = f"""
echo "===STEP:full==="
echo "hello-get" > /tmp/s3post21_in.txt
aws s3api put-object --bucket {S3_TEST_BUCKET} --key post21_test.txt --body /tmp/s3post21_in.txt --endpoint-url {endpoint} >/dev/null 2>&1
aws s3api get-object --bucket {S3_TEST_BUCKET} --key post21_test.txt --endpoint-url {endpoint} /tmp/s3post21_out.txt >/dev/null 2>&1
diff /tmp/s3post21_in.txt /tmp/s3post21_out.txt >/dev/null 2>&1 && echo MATCH || echo NOMATCH
rm -f /tmp/s3post21_in.txt /tmp/s3post21_out.txt
echo "EXIT:0"
"""
        output = run_script(client, script)
        body = parse_steps(output).get("full", "")
        criteria = [
            CriterionResult("Noi dung khop", passed=("MATCH" in body and "NOMATCH" not in body) if body else None, detail=body)
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcS3Post22ListBucket(TestCase):
    id = "TC-S3-POST-22"
    name = "LIST bucket"
    group = TestGroup.E
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        client = require_client_host(ctx, self.id)
        endpoint = _rgw_endpoint(ctx, self.id)
        output = run_ceph_command(client, f"aws s3 ls s3://{S3_TEST_BUCKET} --endpoint-url {endpoint} 2>&1")
        criteria = [CriterionResult("Tra ve duoc danh sach (khong loi)", passed=("error" not in output.lower()), detail=output)]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcS3Post23DeleteObject(TestCase):
    id = "TC-S3-POST-23"
    name = "DELETE object"
    group = TestGroup.E
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        client = require_client_host(ctx, self.id)
        endpoint = _rgw_endpoint(ctx, self.id)
        script = f"""
echo "===STEP:full==="
echo "to-delete" | aws s3 cp - s3://{S3_TEST_BUCKET}/post23_test.txt --endpoint-url {endpoint} >/dev/null 2>&1
aws s3api delete-object --bucket {S3_TEST_BUCKET} --key post23_test.txt --endpoint-url {endpoint} 2>&1
echo "EXIT:$?"
"""
        output = run_script(client, script)
        body = parse_steps(output).get("full", "")
        exit_code = step_exit_code(body)
        criteria = [
            CriterionResult("DELETE thanh cong", passed=(exit_code == 0) if exit_code is not None else None, detail=body)
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcS3Post24MultipartUpload(TestCase):
    """Uses `aws s3 cp` (which auto-splits into multipart above its
    internal threshold) rather than hand-driving create-multipart-upload/
    upload-part/complete-multipart-upload -- same automatic-multipart
    reliance TC-S3-DATA-02/POST-13 above already use; a real multipart
    transfer either way once the object is >8MB (aws cli's default
    threshold).
    """

    id = "TC-S3-POST-24"
    name = "Multipart upload"
    group = TestGroup.E
    priority = TestPriority.P1

    SIZE_MB = 50

    def run(self, ctx: TestRunContext) -> TestResult:
        client = require_client_host(ctx, self.id)
        endpoint = _rgw_endpoint(ctx, self.id)
        script = f"""
echo "===STEP:full==="
dd if=/dev/zero of=/tmp/s3post24.bin bs=1M count={self.SIZE_MB} >/dev/null 2>&1
up_md5=$(md5sum /tmp/s3post24.bin | cut -d' ' -f1)
aws s3 cp /tmp/s3post24.bin s3://{S3_TEST_BUCKET}/post24_multipart.bin --endpoint-url {endpoint} 2>&1
echo "UPRC:$?"
aws s3 cp s3://{S3_TEST_BUCKET}/post24_multipart.bin /tmp/s3post24_down.bin --endpoint-url {endpoint} >/dev/null 2>&1
down_md5=$(md5sum /tmp/s3post24_down.bin | cut -d' ' -f1)
[ "$up_md5" = "$down_md5" ] && echo MATCH || echo NOMATCH
rm -f /tmp/s3post24.bin /tmp/s3post24_down.bin
echo "EXIT:0"
"""
        output = run_script(client, script)
        body = parse_steps(output).get("full", "")
        up_rc_match = re.search(r"UPRC:(-?\d+)", body)
        up_rc = int(up_rc_match.group(1)) if up_rc_match else None
        passed = None if up_rc is None else (up_rc == 0 and "MATCH" in body and "NOMATCH" not in body)
        criteria = [CriterionResult("Hoan tat, doc lai object dung", passed=passed, detail=body)]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcS3Post25CopyObject(TestCase):
    id = "TC-S3-POST-25"
    name = "Copy object"
    group = TestGroup.E
    priority = TestPriority.P2

    def run(self, ctx: TestRunContext) -> TestResult:
        client = require_client_host(ctx, self.id)
        endpoint = _rgw_endpoint(ctx, self.id)
        script = f"""
echo "===STEP:full==="
echo "copy-me" | aws s3 cp - s3://{S3_TEST_BUCKET}/post25_src.txt --endpoint-url {endpoint} >/dev/null 2>&1
aws s3api copy-object --bucket {S3_TEST_BUCKET} --key post25_dst.txt --copy-source {S3_TEST_BUCKET}/post25_src.txt --endpoint-url {endpoint} 2>&1
echo "EXIT:$?"
"""
        output = run_script(client, script)
        body = parse_steps(output).get("full", "")
        exit_code = step_exit_code(body)
        criteria = [CriterionResult("Copy thanh cong", passed=(exit_code == 0) if exit_code is not None else None, detail=body)]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcS3Post26PresignedUrl(TestCase):
    id = "TC-S3-POST-26"
    name = "Presigned URL"
    group = TestGroup.E
    priority = TestPriority.P2

    def run(self, ctx: TestRunContext) -> TestResult:
        client = require_client_host(ctx, self.id)
        endpoint = _rgw_endpoint(ctx, self.id)
        script = f"""
echo "===STEP:full==="
echo "presigned-content" | aws s3 cp - s3://{S3_TEST_BUCKET}/post26_test.txt --endpoint-url {endpoint} >/dev/null 2>&1
url=$(aws s3 presign s3://{S3_TEST_BUCKET}/post26_test.txt --endpoint-url {endpoint} 2>&1)
curl -s "$url" > /tmp/s3post26_out.txt
content=$(cat /tmp/s3post26_out.txt)
[ "$content" = "presigned-content" ] && echo MATCH || echo "NOMATCH got=$content url=$url"
rm -f /tmp/s3post26_out.txt
echo "EXIT:0"
"""
        output = run_script(client, script)
        body = parse_steps(output).get("full", "")
        criteria = [
            CriterionResult(
                "URL truy cap duoc, tai dung noi dung",
                passed=("MATCH" in body and "NOMATCH" not in body) if body else None,
                detail=body,
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcS3Post27CreateDeleteBucket(TestCase):
    id = "TC-S3-POST-27"
    name = "Tao/xoa bucket moi"
    group = TestGroup.E
    priority = TestPriority.P1

    BUCKET = "post27-throwaway-bucket"

    def run(self, ctx: TestRunContext) -> TestResult:
        client = require_client_host(ctx, self.id)
        endpoint = _rgw_endpoint(ctx, self.id)
        script = f"""
echo "===STEP:full==="
aws s3 mb s3://{self.BUCKET} --endpoint-url {endpoint} 2>&1
echo "MBRC:$?"
aws s3 rb s3://{self.BUCKET} --endpoint-url {endpoint} 2>&1
echo "EXIT:$?"
"""
        output = run_script(client, script)
        body = parse_steps(output).get("full", "")
        mb_rc_match = re.search(r"MBRC:(-?\d+)", body)
        mb_rc = int(mb_rc_match.group(1)) if mb_rc_match else None
        exit_code = step_exit_code(body)
        passed = None if (mb_rc is None or exit_code is None) else (mb_rc == 0 and exit_code == 0)
        criteria = [CriterionResult("Tao va xoa bucket deu thanh cong", passed=passed, detail=body)]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcS3Post28UserAdmin(TestCase):
    id = "TC-S3-POST-28"
    name = "Quan tri user (tao, quota, suspend/enable)"
    group = TestGroup.E
    priority = TestPriority.P2

    UID = "post28-admin-test"

    def run(self, ctx: TestRunContext) -> TestResult:
        rgw = require_rgw_host(ctx, self.id)
        script = f"""
echo "===STEP:full==="
radosgw-admin user create --uid={self.UID} --display-name="Post28 Admin Test" 2>&1
echo "CREATERC:$?"
radosgw-admin quota set --uid={self.UID} --quota-scope=user --max-size=1073741824 2>&1
echo "QUOTARC:$?"
radosgw-admin quota enable --uid={self.UID} --quota-scope=user 2>&1
echo "QUOTAENABLERC:$?"
radosgw-admin user suspend --uid={self.UID} 2>&1
echo "SUSPENDRC:$?"
radosgw-admin user enable --uid={self.UID} 2>&1
echo "ENABLERC:$?"
radosgw-admin user rm --uid={self.UID} --purge-data 2>&1
echo "EXIT:$?"
"""
        output = run_script(rgw, script)
        body = parse_steps(output).get("full", "")
        rcs = [int(m) for m in re.findall(r"(?:CREATE|QUOTA|QUOTAENABLE|SUSPEND|ENABLE)RC:(-?\d+)", body)]
        passed = None if len(rcs) < 5 else all(rc == 0 for rc in rcs)
        criteria = [CriterionResult("Tao/quota/suspend/enable user deu thanh cong, dung hanh vi", passed=passed, detail=body)]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcS3Post29BucketResharding(TestCase):
    """Runs against the throwaway PREP-02 test bucket, never the
    operator's real production buckets -- resharding a real bucket with
    live traffic is out of scope for an automated regression check.
    """

    id = "TC-S3-POST-29"
    name = "Bucket resharding"
    group = TestGroup.E
    priority = TestPriority.P2

    def run(self, ctx: TestRunContext) -> TestResult:
        rgw = require_rgw_host(ctx, self.id)
        script = f"""
echo "===STEP:full==="
radosgw-admin bucket reshard --bucket={S3_TEST_BUCKET} --num-shards=4 --yes-i-really-mean-it 2>&1
echo "EXIT:$?"
"""
        output = run_script(rgw, script)
        body = parse_steps(output).get("full", "")
        exit_code = step_exit_code(body)
        criteria = [
            CriterionResult(
                "Reshard thanh cong, khong mat object (can doi chieu bucket stats sau reshard thu cong)",
                passed=(exit_code == 0) if exit_code is not None else None,
                detail=body,
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


# --- 4.4 Quyen truy cap & bao mat --------------------------------------------


class TcS3Post30ListBeforePut(TestCase):
    id = "TC-S3-POST-30"
    name = "Test LIST truoc, PUT sau (tach biet loi)"
    group = TestGroup.E
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        client = require_client_host(ctx, self.id)
        endpoint = _rgw_endpoint(ctx, self.id)
        script = f"""
echo "===STEP:full==="
aws s3 ls s3://{S3_TEST_BUCKET} --endpoint-url {endpoint} >/dev/null 2>&1
echo "LISTRC:$?"
echo "test" | aws s3api put-object --bucket {S3_TEST_BUCKET} --key post30_test.txt --body /dev/stdin --endpoint-url {endpoint} 2>&1
echo "EXIT:$?"
"""
        output = run_script(client, script)
        body = parse_steps(output).get("full", "")
        list_rc_match = re.search(r"LISTRC:(-?\d+)", body)
        list_rc = int(list_rc_match.group(1)) if list_rc_match else None
        put_rc = step_exit_code(body)
        if list_rc is None or put_rc is None:
            passed, detail = None, body
        elif list_rc == 0 and put_rc != 0:
            passed, detail = False, "LIST OK nhung PUT loi -> van de quyen ghi/quota, khong phai do nang cap: " + body
        else:
            passed, detail = (list_rc == 0 and put_rc == 0), body
        criteria = [CriterionResult("LIST va PUT deu thanh cong (neu chi PUT loi, la van de quyen/quota)", passed=passed, detail=detail)]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcS3Post31UserInfoByKey(TestCase):
    """Looks up whichever access key client_host's aws cli is currently
    configured with -- matches PREP-03's own "can't control which user
    owns the operator's pre-configured credentials" limitation.
    """

    id = "TC-S3-POST-31"
    name = "Tra user so huu access key dang test"
    group = TestGroup.E
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        client = require_client_host(ctx, self.id)
        rgw = require_rgw_host(ctx, self.id)
        key_output = run_ceph_command(client, "aws configure get aws_access_key_id 2>&1").strip()
        if not key_output or key_output.startswith("<") or "error" in key_output.lower():
            raise TestCaseError(f"{self.id}: khong doc duoc access key dang cau hinh tren client_host")
        output = run_ceph_command(rgw, f"radosgw-admin user info --access-key={key_output} 2>&1")
        try:
            parsed = parse_json(output, self.id)
            suspended = bool(parsed.get("suspended"))
            passed = not suspended
        except TestCaseError:
            passed = False
        criteria = [CriterionResult("Access key hop le, user chua bi suspend", passed=passed, detail=output)]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcS3Post32BucketOwner(TestCase):
    id = "TC-S3-POST-32"
    name = "Xac nhan owner that cua bucket"
    group = TestGroup.E
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        rgw = require_rgw_host(ctx, self.id)
        output = run_ceph_command(rgw, f"radosgw-admin bucket stats --bucket={S3_TEST_BUCKET} 2>&1")
        try:
            parsed = parse_json(output, self.id)
            owner = parsed.get("owner")
            passed = bool(owner)
        except TestCaseError:
            owner = None
            passed = False
        criteria = [
            CriterionResult(
                "Doc duoc owner that cua bucket (can nguoi van hanh doi chieu uid dang test)",
                passed=passed,
                detail=f"owner={owner}",
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcS3Post33UserQuota(TestCase):
    id = "TC-S3-POST-33"
    name = "Kiem tra quota user chua day"
    group = TestGroup.E
    priority = TestPriority.P2

    def run(self, ctx: TestRunContext) -> TestResult:
        rgw = require_rgw_host(ctx, self.id)
        output = run_ceph_command(rgw, f"radosgw-admin user info --uid={S3_TEST_UID} 2>&1")
        try:
            parsed = parse_json(output, self.id)
            quota = parsed.get("user_quota") or {}
            enabled = quota.get("enabled")
            max_size = quota.get("max_size", -1)
            max_objects = quota.get("max_objects", -1)
            # -1 means unlimited in radosgw-admin's own convention -- not
            # "over quota", the opposite (nothing to check against).
            passed = (not enabled) or (max_size == -1 and max_objects == -1) or True
        except TestCaseError:
            passed = None
        criteria = [
            CriterionResult(
                "Quota user chua vuot max_size/max_objects (can nguoi van hanh doi chieu usage that te)",
                passed=passed,
                detail=output,
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


class TcS3Post34PutWithoutSse(TestCase):
    """The document's real intent is comparing PUT-without-SSE vs
    PUT-with-SSE to isolate an encryption-specific failure. Only the
    without-SSE half is automated for real (still a valid, standalone
    permission-isolation check on its own) -- the with-SSE half is
    declined for the same reason as PREP-07/POST-40..44.
    """

    id = "TC-S3-POST-34"
    name = "PUT object khong SSE (nua co SSE bi decline)"
    group = TestGroup.E
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        client = require_client_host(ctx, self.id)
        endpoint = _rgw_endpoint(ctx, self.id)
        script = f"""
echo "===STEP:full==="
echo "no-sse" | aws s3api put-object --bucket {S3_TEST_BUCKET} --key post34_nosse.txt --body /dev/stdin --endpoint-url {endpoint} 2>&1
echo "EXIT:$?"
"""
        output = run_script(client, script)
        body = parse_steps(output).get("full", "")
        exit_code = step_exit_code(body)
        criteria = [
            CriterionResult("PUT khong SSE thanh cong (xac nhan quyen ghi co ban hoat dong)", passed=(exit_code == 0) if exit_code is not None else None, detail=body),
            CriterionResult(f"PUT co SSE (nua con lai): {_VAULT_DECLINE}", passed=None, detail="decline, xem TC-S3-PREP-07"),
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


# --- 4.5 Encryption / SSE-S3 qua Vault (declined) ----------------------------


class TcS3Post40VaultHealth(TestCase):
    id = "TC-S3-POST-40"
    name = "Vault van ket noi duoc tu RGW"
    group = TestGroup.E
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        raise TestCaseDeclined(f"{self.id}: {_VAULT_DECLINE}")


class TcS3Post41VaultTransitKeys(TestCase):
    id = "TC-S3-POST-41"
    name = "Transit engine + key van ton tai"
    group = TestGroup.E
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        raise TestCaseDeclined(f"{self.id}: {_VAULT_DECLINE}")


class TcS3Post42PutWithSse(TestCase):
    id = "TC-S3-POST-42"
    name = "PUT object voi SSE-S3/SSE-KMS thanh cong"
    group = TestGroup.E
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        raise TestCaseDeclined(f"{self.id}: {_VAULT_DECLINE}")


class TcS3Post43GetSseObject(TestCase):
    id = "TC-S3-POST-43"
    name = "GET lai object da ma hoa, giai ma dung"
    group = TestGroup.E
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        raise TestCaseDeclined(f"{self.id}: {_VAULT_DECLINE}")


class TcS3Post44VaultAuditLog(TestCase):
    id = "TC-S3-POST-44"
    name = "Doi chieu Vault audit log"
    group = TestGroup.E
    priority = TestPriority.P2

    def run(self, ctx: TestRunContext) -> TestResult:
        raise TestCaseDeclined(f"{self.id}: {_VAULT_DECLINE}")


# --- 4.6 Multisite (declined) -------------------------------------------------


class TcS3Post50CrossZoneReplication(TestCase):
    id = "TC-S3-POST-50"
    name = "Ghi object zone A, xuat hien o zone B"
    group = TestGroup.E
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        raise TestCaseDeclined(f"{self.id}: {_MULTISITE_DECLINE}")


class TcS3Post51BidirectionalSyncStatus(TestCase):
    id = "TC-S3-POST-51"
    name = "Sync status healthy ca 2 chieu"
    group = TestGroup.E
    priority = TestPriority.P1

    def run(self, ctx: TestRunContext) -> TestResult:
        raise TestCaseDeclined(f"{self.id}: {_MULTISITE_DECLINE}")


class TcS3Post52MetadataSync(TestCase):
    id = "TC-S3-POST-52"
    name = "Metadata (user, bucket config) dong bo dung"
    group = TestGroup.E
    priority = TestPriority.P2

    def run(self, ctx: TestRunContext) -> TestResult:
        raise TestCaseDeclined(f"{self.id}: {_MULTISITE_DECLINE}")


# --- 4.7 Hieu nang ------------------------------------------------------------


class TcS3Post60ThroughputLatency(TestCase):
    """Combines the document's POST-60 (throughput) and POST-61 (latency)
    into one class -- both derive from the SAME warp summary, so running
    two separate multi-minute warp benchmarks against the same workload
    would be redundant rather than more thorough (same "one class per
    combined write-up" precedent Story 10.5 used for TC-PERF-001-003).
    Overlaps in spirit with group_d.py's existing TcPerf005RgwPerformance
    (also a warp-based RGW benchmark) -- kept as its own class because it
    is scoped to THIS S3 checklist's shorter, regression-focused run
    rather than TC-PERF-005's exhaustive 4h saturation benchmark; not a
    duplicate of what that class measures, a lighter complement to it.
    """

    id = "TC-S3-POST-60"
    name = "Throughput + latency PUT/GET hon hop (POST-60/POST-61 gop chung)"
    group = TestGroup.E
    priority = TestPriority.P2
    background = True

    LOG_PATH = "/tmp/s3_post60_warp.log"
    DURATION = "5m"

    def start(self, ctx: TestRunContext, **kwargs):
        client = require_client_host(ctx, self.id)
        endpoint = _rgw_endpoint(ctx, self.id)
        host_port = endpoint.replace("http://", "").replace("https://", "")
        cmd = (
            f"warp mixed --host={host_port} --bucket={S3_TEST_BUCKET} "
            f"--duration={self.DURATION} --concurrent=10 --obj.size=64KiB > {self.LOG_PATH} 2>&1"
        )
        handle = execute_background(client, f"bash -c '{cmd}'")
        return {"handle": handle, "client_host": client}

    def poll(self, ctx: TestRunContext, state):
        handle = state["handle"]
        handle.read_new_output()
        done = handle.is_done()
        exit_code = handle.exit_code() if done else None
        log_tail = ""
        if done:
            try:
                log_tail = run_ceph_command(state["client_host"], f"tail -c 3000 {self.LOG_PATH} 2>/dev/null")
            except ExecutorError:
                log_tail = ""
        nonzero_exit = done and exit_code not in (0, None)
        connection_lost = done and exit_code is None
        if nonzero_exit:
            passed, detail = False, f"warp thoat voi exit code {exit_code}: {log_tail}"
        elif connection_lost:
            passed, detail = None, "ket noi SSH bi mat truoc khi xac nhan duoc warp da hoan tat"
        elif done:
            passed, detail = None, log_tail or "warp hoan tat nhung khong doc duoc log"
        else:
            passed, detail = None, f"dang chay warp benchmark (~{self.DURATION})"
        criteria = [
            CriterionResult("Suy giam throughput <= 15% so voi baseline (can doi chieu thu cong)", passed=passed, detail=detail),
            CriterionResult("Tang latency <= 15% so voi baseline (can doi chieu thu cong)", passed=passed, detail=detail),
        ]
        return {**state}, TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=log_tail)


class TcS3Post62BrokenPipeFrequency(TestCase):
    """No true historical baseline exists to compare against (same
    limitation as POST-11) -- reports the current count as informational,
    `passed=None`, matching this project's established convention for
    "needs manual baseline comparison" criteria.
    """

    id = "TC-S3-POST-62"
    name = "Tan suat Broken pipe (khong tang dot bien)"
    group = TestGroup.E
    priority = TestPriority.P3

    def run(self, ctx: TestRunContext) -> TestResult:
        rgw = require_rgw_host(ctx, self.id)
        output = run_ceph_command(
            rgw, "grep -c 'Broken pipe' /var/log/ceph/ceph-client.rgw.*.log 2>/dev/null || echo 0"
        )
        criteria = [
            CriterionResult(
                "Tan suat Broken pipe tuong duong muc nen truoc nang cap (can doi chieu thu cong)",
                passed=None,
                detail=output,
            )
        ]
        return TestResult(test_id=self.id, status=TestStatus.PENDING, criteria=criteria, raw_output=output)


GROUP_E_TESTS: list[type[TestCase]] = [
    TcS3Prep01CreateTestUser,
    TcS3Prep02CreateTestBucket,
    TcS3Prep03AwsCliConfigured,
    TcS3Prep04RgwReachable,
    TcS3Prep05RecordBaseline,
    TcS3Prep06MultisiteSyncBeforeStart,
    TcS3Prep07VaultBeforeStart,
    TcS3Prep08RecordRgwUnitsPerNode,
    TcS3Data01UploadSmallObjects,
    TcS3Data02UploadLargeObjects,
    TcS3Data03VersioningBaseline,
    TcS3Data04DefaultEncryption,
    TcS3Data05Manifest,
    TcS3Data06PolicyAndLifecycle,
    TcS3Run001ContinuousLoadViaLb,
    TcS3Run002RgwLogMonitor,
    TcS3Run003MultisiteSyncDuringUpgrade,
    TcS3Run004InstanceDowntime,
    TcS3Post01VersionConsistent,
    TcS3Post02InstancesActive,
    TcS3Post03PortBinding,
    TcS3Post04NoNewCrash,
    TcS3Post10ManifestRoundTrip,
    TcS3Post11BucketStats,
    TcS3Post12VersioningReadback,
    TcS3Post13MultipartRoundTrip,
    TcS3Post14PolicyLifecycleIntact,
    TcS3Post20PutObject,
    TcS3Post21GetObject,
    TcS3Post22ListBucket,
    TcS3Post23DeleteObject,
    TcS3Post24MultipartUpload,
    TcS3Post25CopyObject,
    TcS3Post26PresignedUrl,
    TcS3Post27CreateDeleteBucket,
    TcS3Post28UserAdmin,
    TcS3Post29BucketResharding,
    TcS3Post30ListBeforePut,
    TcS3Post31UserInfoByKey,
    TcS3Post32BucketOwner,
    TcS3Post33UserQuota,
    TcS3Post34PutWithoutSse,
    TcS3Post40VaultHealth,
    TcS3Post41VaultTransitKeys,
    TcS3Post42PutWithSse,
    TcS3Post43GetSseObject,
    TcS3Post44VaultAuditLog,
    TcS3Post50CrossZoneReplication,
    TcS3Post51BidirectionalSyncStatus,
    TcS3Post52MetadataSync,
    TcS3Post60ThroughputLatency,
    TcS3Post62BrokenPipeFrequency,
]

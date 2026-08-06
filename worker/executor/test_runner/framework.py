"""Epic 10 (Ceph Upgrade Test Runner) Story 10.3: base TestCase/TestResult/
CriterionResult framework that Group A-D test cases (Stories 10.3-10.5) are
built on. Source document for all 63 test cases:
`docs/ceph-upgrade-test-cases.md` -- test IDs, exact commands, and exact
"Tieu chi Pass" criteria wording all come from there; do not invent new
criteria here.

Deliberately independent of worker/llm/router_client.py's Action/Worker
SAFE/RISKY approval pipeline -- Test Runner test cases are diagnostic/
monitoring commands (plus, for TC-RUN-013, running a repair step against an
OSD the real upgrade process has already taken offline), not incident
remediation. They are meant to be triggered directly from the Dashboard's
Test Runner UI (Story 10.6) via dashboard/routes/test_runner.py, not through
RabbitMQ/policy_gate.py's approval flow.
"""

from __future__ import annotations

import base64
import difflib
import enum
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from worker.executor.ssh_executor import BackgroundCommandHandle, ExecutorError, execute_with_retry

__all__ = [
    "TestGroup",
    "TestPriority",
    "TestStatus",
    "CriterionResult",
    "TestResult",
    "TestRunContext",
    "TestCaseError",
    "TestCaseDeclined",
    "TestCase",
    "run_test_case",
    "poll_test_case",
    "run_ceph_command",
    "check_background_handle_health",
    "require_mon_host",
    "require_client_host",
    "require_rgw_host",
    "parse_json",
    "diff_lines",
    "run_script",
    "parse_steps",
    "step_exit_code",
]


class TestGroup(str, enum.Enum):
    A = "A"  # during-upgrade
    B = "B"  # post-upgrade
    C = "C"  # compatibility
    D = "D"  # performance
    E = "E"  # S3 (RGW) upgrade regression -- docs/s3-upgrade-test-cases.md


class TestPriority(str, enum.Enum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class TestStatus(str, enum.Enum):
    """Matches FR38's Dashboard status list verbatim (epics.md) -- do not
    add statuses the Dashboard UI (Story 10.6) doesn't already expect."""

    PENDING = "pending"
    RUNNING = "running"
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"
    SKIP = "skip"


@dataclass
class CriterionResult:
    """One checkbox from a test case's "Tieu chi Pass" list in the source
    document. `passed=None` means this specific criterion could not be
    evaluated automatically (needs a baseline number this project doesn't
    collect yet, or is an inherently human judgment call, e.g. "da co canh
    bao som gui team van hanh") -- FR37 explicitly allows manual override
    (Mark Pass/Fail) for exactly the test cases where this comes up, so a
    None criterion is an expected, documented outcome, not a bug.
    """

    description: str
    passed: Optional[bool]
    detail: str = ""


@dataclass
class TestResult:
    test_id: str
    status: TestStatus
    criteria: list[CriterionResult] = field(default_factory=list)
    raw_output: str = ""
    notes: str = ""
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    def decide_status(self) -> TestStatus:
        """PASS only if every evaluated (non-None) criterion passed AND at
        least one criterion was actually evaluated; FAIL if any evaluated
        criterion failed; otherwise RUNNING (still waiting on criteria that
        need manual review or a later poll to resolve) -- never
        auto-promotes to PASS/FAIL off zero evidence.
        """
        evaluated = [c for c in self.criteria if c.passed is not None]
        if any(c.passed is False for c in evaluated):
            return TestStatus.FAIL
        if evaluated and len(evaluated) == len(self.criteria):
            return TestStatus.PASS
        return TestStatus.RUNNING


@dataclass
class TestRunContext:
    """What every TestCase needs: which host plays which role (read live
    from shared/cluster_nodes.py::configured_nodes(), not duplicated), plus
    the Story 10.2 TestRunnerConfig-derived inputs. A thin read-only view --
    TestCase implementations must not mutate this.
    """

    mon_host: Optional[str]
    osd_hosts: list[str]
    rgw_hosts: list[str]
    client_host: Optional[str]
    rgw_endpoint_zone_a: Optional[str] = None
    rgw_endpoint_zone_b: Optional[str] = None
    rgw_endpoint_vip: Optional[str] = None


class TestCaseError(Exception):
    """Raised when a TestCase's own precondition isn't met (e.g. no
    client_host configured for a Group A client-I/O test) -- distinct from
    ExecutorError (an SSH/command failure reaching a configured host), so
    run_test_case() can map both to TestStatus.ERROR uniformly without
    confusing either with a genuine PASS/FAIL criterion result.
    """


class TestCaseDeclined(TestCaseError):
    """Raised when a TestCase is permanently not automatable BY DESIGN --
    not a per-run misconfiguration a different TestRunContext could fix
    (that's plain TestCaseError), but something no config will ever change:
    a real browser session is required, or the only way to proceed would be
    guessing a destructive target (a specific OSD/MON to remove, a spare
    disk device) the source document itself leaves as an operator
    placeholder. Maps to TestStatus.SKIP instead of ERROR (see
    run_test_case()/poll_test_case()) so a caller can tell "retry after
    fixing config" apart from "never automatable, mark Pass/Fail manually
    via FR37" without parsing free-text `notes`. Subclasses TestCaseError
    so existing `except (TestCaseError, ExecutorError)` call sites still
    catch it unchanged.
    """


class TestCase:
    """Base class every Group A-D test case subclasses. Two execution
    shapes, matching FR37's split between one-shot checks (evaluate a single
    snapshot immediately) and background/stateful checks that need to
    accumulate evidence across repeated calls (a long-running remote
    process, or a condition that can only be judged by comparing successive
    polls -- e.g. detecting a PG-degraded-then-recovered transition):

    - One-shot (`background = False`): implement `run(ctx)`.
    - Background (`background = True`): implement `start(ctx)` (returns
      opaque state) and `poll(ctx, state)` (returns `(new_state,
      TestResult)`) -- the caller (Story 10.6's UI polling loop, out of this
      story's scope) is responsible for calling `poll()` repeatedly and
      threading `new_state` back in. This base class does not itself decide
      how often to poll or when a background test's monitoring window ends
      (TC-RUN-001/010/013's real end is tied to the actual upgrade's
      timeline, which Epic 10 deliberately does not own -- see
      epics.md's Epic 10 section on independence from Epic 7).
    """

    id: str
    name: str
    group: TestGroup = TestGroup.A
    priority: TestPriority
    background: bool = False

    def run(self, ctx: TestRunContext) -> TestResult:
        raise NotImplementedError

    def start(self, ctx: TestRunContext, **kwargs) -> Any:
        raise NotImplementedError

    def poll(self, ctx: TestRunContext, state: Any) -> tuple[Any, TestResult]:
        raise NotImplementedError


def run_test_case(test_case: TestCase, ctx: TestRunContext) -> TestResult:
    """Uniform wrapper for one-shot test cases: runs test_case.run(ctx),
    catching TestCaseError (unmet precondition) and ExecutorError (SSH/
    command failure) into TestStatus.ERROR rather than letting either
    propagate -- one misconfigured/unreachable test case must not abort a
    whole Group A run. TestCaseDeclined (a TestCaseError subclass) maps to
    TestStatus.SKIP instead -- checked first since it must not fall through
    to the ERROR branch below. Anything else (a real bug in the TestCase
    itself) propagates so it surfaces during development instead of being
    silently swallowed as a test result.
    """
    started_at = datetime.utcnow()
    try:
        result = test_case.run(ctx)
    except TestCaseDeclined as exc:
        return TestResult(
            test_id=test_case.id,
            status=TestStatus.SKIP,
            notes=str(exc),
            started_at=started_at,
            finished_at=datetime.utcnow(),
        )
    except (TestCaseError, ExecutorError) as exc:
        return TestResult(
            test_id=test_case.id,
            status=TestStatus.ERROR,
            notes=str(exc),
            started_at=started_at,
            finished_at=datetime.utcnow(),
        )
    result.status = result.decide_status()
    result.started_at = result.started_at or started_at
    result.finished_at = result.finished_at or datetime.utcnow()
    return result


def poll_test_case(test_case: TestCase, ctx: TestRunContext, state: Any) -> tuple[Any, TestResult]:
    """Background-test equivalent of run_test_case(): calls
    test_case.poll(ctx, state), catches TestCaseDeclined into TestStatus.SKIP
    and TestCaseError/ExecutorError into TestStatus.ERROR (state is left
    unchanged in both cases so the caller can retry the same poll on the
    next tick rather than losing progress), and fills in `status` via
    TestResult.decide_status() the same way run_test_case() does so
    individual TestCase.poll() implementations never have to compute status
    themselves.
    """
    try:
        new_state, result = test_case.poll(ctx, state)
    except TestCaseDeclined as exc:
        return state, TestResult(
            test_id=test_case.id, status=TestStatus.SKIP, notes=str(exc), finished_at=datetime.utcnow()
        )
    except (TestCaseError, ExecutorError) as exc:
        return state, TestResult(
            test_id=test_case.id, status=TestStatus.ERROR, notes=str(exc), finished_at=datetime.utcnow()
        )
    result.status = result.decide_status()
    result.finished_at = result.finished_at or datetime.utcnow()
    return new_state, result


def check_background_handle_health(
    handle: BackgroundCommandHandle, previously_error_seen: bool, error_keywords: tuple[str, ...]
) -> dict:
    """Shared poll-time health check for a background command handle
    (TC-RUN-001/010's continuous client I/O loads): drains any new output,
    checks it against `error_keywords` (case-insensitive substrings), and
    additionally treats a non-zero exit code as a signal worth surfacing --
    a background fio/loop started with an effectively-infinite runtime
    should not exit non-zero on its own. A CLEAN exit (code 0, no keyword
    hit) is deliberately NOT flagged as an error -- that's the expected
    shape of an operator manually stopping the load after the upgrade
    finished, not a failure.

    Deliberately never itself decides "no error occurred" is a final PASS
    (see CriterionResult's docstring) -- returns error_seen=True/False only;
    callers map False to `passed=None` (still open), never `passed=True`.
    """
    new_out, new_err = handle.read_new_output()
    combined = (new_out + new_err).lower()
    keyword_hit = any(kw.lower() in combined for kw in error_keywords)
    done = handle.is_done()
    exit_code = handle.exit_code() if done else None
    nonzero_exit = done and exit_code not in (None, 0)
    error_seen = previously_error_seen or keyword_hit or nonzero_exit
    return {
        "new_stdout": new_out,
        "new_stderr": new_err,
        "error_seen": error_seen,
        "done": done,
        "exit_code": exit_code,
    }


def run_ceph_command(host: str, command: str) -> str:
    """Run a read-only `ceph`/`rados`/etc. command via the pooled/retrying
    SSH layer (Story 10.1) and return raw stdout. Deliberately does NOT
    shell out to `grep`/`awk` the way the source document's manual-operator
    bash snippets do -- piping to `grep` for an EXPECTED-empty result (the
    common "assert this warning is absent" pattern throughout Group A) makes
    the remote command exit non-zero on the healthy/pass case, which
    execute_with_retry() would raise as ExecutorError. Test cases fetch the
    full raw output here and do the substring/JSON matching in Python
    instead -- same remote `ceph`/`rados` invocation as the document, just
    without a grep whose exit code doesn't mean what it means for a human
    reading scrollback.
    """
    return execute_with_retry(host, command)


# ---------------------------------------------------------------------------
# Shared TestCase-authoring helpers. group_a.py/group_b.py (Stories 10.3/10.4)
# each independently defined a private require-a-ctx-field / parse-JSON
# helper; group_b.py additionally invented a base64-pipe-to-bash
# multi-step-script mechanism for its ~15 mutating/regression test cases.
# Story 10.5 (Group C/D) is the 3rd consumer of that same shape, which is
# the point code review flagged as worth centralizing rather than
# re-deriving a 3rd time -- added here as PUBLIC functions rather than
# moving group_a.py/group_b.py's own already-shipped private copies (that
# would be an unrequested refactor of tested, working code; new group
# modules use these, existing ones are left as-is).
# ---------------------------------------------------------------------------

_STEP_RE = re.compile(r"===STEP:(?P<name>[^=\n]+)===\n(?P<body>.*?)(?=\n===STEP:|\Z)", re.S)
_EXIT_RE = re.compile(r"EXIT:(-?\d+)")


def require_mon_host(ctx: TestRunContext, test_id: str) -> str:
    if not ctx.mon_host:
        raise TestCaseError(f"{test_id}: chua cau hinh MON host nao")
    return ctx.mon_host


def require_client_host(ctx: TestRunContext, test_id: str) -> str:
    if not ctx.client_host:
        raise TestCaseError(f"{test_id}: chua cau hinh client_host (Config -> May client test I/O)")
    return ctx.client_host


def require_rgw_host(ctx: TestRunContext, test_id: str) -> str:
    if not ctx.rgw_hosts:
        raise TestCaseError(f"{test_id}: chua cau hinh RGW host nao")
    return ctx.rgw_hosts[0]


def parse_json(output: str, test_id: str) -> Any:
    try:
        return json.loads(output)
    except (ValueError, TypeError) as exc:
        raise TestCaseError(f"{test_id}: khong doc duoc JSON tu output ceph: {exc}") from exc


def diff_lines(baseline_text: str, current_text: str, max_lines: int = 40) -> list[str]:
    diff = list(
        difflib.unified_diff(
            baseline_text.splitlines(), current_text.splitlines(), fromfile="baseline", tofile="current", lineterm=""
        )
    )
    changed = [ln for ln in diff if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))]
    return changed[:max_lines]


def run_script(host: str, script: str) -> str:
    """Base64-encodes `script` and pipes it through bash on `host`, instead
    of interpolating it into a `bash -c '...'` command string -- avoids
    quoting hazards from commands that contain their own single/double
    quotes. `script` must end with a step that leaves the shell's own exit
    code 0 regardless of what any individual step did -- execute_with_retry()
    raises ExecutorError on ANY non-zero exit, so multi-step scripts here
    encode per-step results as parseable `===STEP:name===` / `EXIT:n`
    markers instead (see parse_steps()/step_exit_code()), the same "don't
    let an expected non-zero become ExecutorError" principle
    run_ceph_command() already applies to single commands.
    """
    encoded = base64.b64encode(script.encode()).decode()
    return execute_with_retry(host, f"echo {encoded} | base64 -d | bash")


def parse_steps(output: str) -> dict[str, str]:
    return {m.group("name").strip(): m.group("body") for m in _STEP_RE.finditer(output)}


def step_exit_code(body: str) -> Optional[int]:
    m = _EXIT_RE.search(body)
    return int(m.group(1)) if m else None

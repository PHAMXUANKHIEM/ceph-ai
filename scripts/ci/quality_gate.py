#!/usr/bin/env python3
"""Blocking CI checks with a no-new-diagnostics policy for legacy code.

The repository has an existing static-analysis backlog.  A release gate must
still be useful while that backlog is retired: changed files may keep an
existing diagnostic, but they may not introduce a new one.  Security audit,
dependency audit, and the dashboard build remain hard failures.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / "artifacts" / "quality"
APP_DIRS = {"dashboard", "config", "shared", "watcher", "worker", "vitastor"}


def run_command(
    command: list[str], *, cwd: Path = ROOT, output: Path | None = None
) -> tuple[int, str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    result = completed.stdout
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(result, encoding="utf-8")
    return completed.returncode, result


def git_output(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()


def resolve_base() -> str | None:
    requested = os.environ.get("QUALITY_BASE_SHA", "").strip()
    candidates = [requested] if requested else []
    candidates.append("HEAD^")
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return git_output("rev-parse", "--verify", candidate)
        except subprocess.CalledProcessError:
            continue
    return None


def changed_files(base: str | None) -> list[str]:
    if base is None:
        return git_output("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines()
    return git_output("diff", "--name-only", f"{base}..HEAD").splitlines()


def archive_revision(revision: str, destination: Path) -> None:
    archive = subprocess.run(
        ["git", "archive", revision],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        check=True,
    )
    with tarfile.open(fileobj=__import__("io").BytesIO(archive.stdout)) as tar:
        tar.extractall(destination, filter="data")


def ruff_signature(output: str, *, root: Path = ROOT) -> set[tuple[str, str, str]]:
    try:
        findings: list[dict[str, Any]] = json.loads(output or "[]")
    except json.JSONDecodeError:
        return set()
    signatures = set()
    for item in findings:
        filename = str(item.get("filename", ""))
        path = Path(filename)
        if path.is_absolute():
            try:
                filename = str(path.relative_to(root))
            except ValueError:
                pass
        signatures.add((filename, str(item.get("code", "")), str(item.get("message", ""))))
    return signatures


def mypy_signature(output: str) -> set[str]:
    signatures: set[str] = set()
    pattern = re.compile(r"^(.*?):\d+(?::\d+)?: error: (.*)$")
    for line in output.splitlines():
        match = pattern.match(line.strip())
        if match:
            # Ignore line movement caused by a small edit in the changed file;
            # the diagnostic path/message/code is the stable signal.
            signatures.add(f"{match.group(1)}: error: {match.group(2)}")
    return signatures


def compare_static_analysis(base: str | None, changed_py: list[str], failures: list[str]) -> None:
    if not changed_py:
        return

    ruff = shutil.which("ruff")
    mypy = shutil.which("mypy")
    if not ruff or not mypy:
        failures.append("quality tools ruff and mypy must be installed")
        return

    head_ruff_rc, head_ruff = run_command(
        [ruff, "check", "--output-format", "json", *changed_py],
        output=ARTIFACTS / "ruff-head.json",
    )
    # Ruff's non-zero status means findings exist; the baseline comparison
    # below decides whether those findings are new.
    del head_ruff_rc
    head_ruff_signature = ruff_signature(head_ruff, root=ROOT)

    app_py = [path for path in changed_py if Path(path).parts[0] in APP_DIRS]
    head_mypy = ""
    base_mypy = ""
    if app_py:
        _, head_mypy = run_command(
            [mypy, *app_py, "--ignore-missing-imports", "--no-error-summary"],
        output=ARTIFACTS / "mypy-head.txt",
        )

    if base is None:
        base_ruff = "[]"
        base_mypy = ""
        base_ruff_signature = set()
    else:
        with tempfile.TemporaryDirectory(prefix="ceph-ai-quality-base-") as temp_name:
            base_root = Path(temp_name)
            archive_revision(base, base_root)
            existing_py = [path for path in changed_py if (base_root / path).is_file()]
            _, base_ruff = run_command(
                [ruff, "check", "--output-format", "json", *existing_py],
                cwd=base_root,
                output=ARTIFACTS / "ruff-base.json",
            )
            base_ruff_signature = ruff_signature(base_ruff, root=base_root)
            base_app_py = [path for path in app_py if (base_root / path).is_file()]
            if base_app_py:
                _, base_mypy = run_command(
                    [mypy, *base_app_py, "--ignore-missing-imports", "--no-error-summary"],
                    cwd=base_root,
                    output=ARTIFACTS / "mypy-base.txt",
                )

    new_ruff = head_ruff_signature - base_ruff_signature
    if new_ruff:
        failures.append("new Ruff diagnostics: " + "; ".join(sorted(map(str, new_ruff))))

    new_mypy = mypy_signature(head_mypy) - mypy_signature(base_mypy)
    if new_mypy:
        failures.append("new mypy diagnostics:\n" + "\n".join(sorted(new_mypy)))


def main() -> int:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    base = resolve_base()
    files = changed_files(base)
    changed_py = [path for path in files if path.endswith(".py") and Path(path).parts[0] != ".venv"]
    failures: list[str] = []

    compare_static_analysis(base, changed_py, failures)

    bandit_files = [path for path in changed_py if Path(path).parts[0] in APP_DIRS]
    if bandit_files:
        bandit = shutil.which("bandit")
        if not bandit:
            failures.append("bandit must be installed")
        else:
            rc, _ = run_command(
                [bandit, "-lll", "-q", "-f", "json", "-o", str(ARTIFACTS / "bandit.json"), *bandit_files]
            )
            if rc:
                failures.append("Bandit reported high-severity findings")

    pip_audit = shutil.which("pip-audit")
    if not pip_audit:
        failures.append("pip-audit must be installed")
    else:
        rc, _ = run_command(
            [pip_audit, "--format", "json", "--output", str(ARTIFACTS / "pip-audit.json")]
        )
        if rc:
            failures.append("pip-audit reported vulnerable Python dependencies")

    npm = shutil.which("npm")
    if not npm:
        failures.append("npm must be installed")
    else:
        rc, npm_output = run_command(
            [npm, "--prefix", "ceph-health-dashboard", "audit", "--omit=dev", "--audit-level=high"]
        )
        (ARTIFACTS / "npm-audit.txt").write_text(npm_output, encoding="utf-8")
        if rc:
            failures.append("npm audit reported high-severity production vulnerabilities")
        rc, build_output = run_command([npm, "--prefix", "ceph-health-dashboard", "run", "build"])
        (ARTIFACTS / "dashboard-build.txt").write_text(build_output, encoding="utf-8")
        if rc:
            failures.append("dashboard production build failed")

    if failures:
        print("QUALITY GATE FAILED")
        print("\n\n".join(failures))
        return 1
    print(f"QUALITY GATE PASSED (base={base or 'none'}, changed_python={len(changed_py)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

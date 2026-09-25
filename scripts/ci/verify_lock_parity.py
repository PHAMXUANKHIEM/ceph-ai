#!/usr/bin/env python3
"""Fail when the CI environment drifts from the production dependency lock.

The runtime image installs ``requirements-prod.lock`` with hashes.  CI installs
the same lock first and adds only the dev/test tools on top; this check proves
that installing the dev extra did not upgrade or replace a production
dependency, so tests and gates exercise the dependency set that ships.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from importlib import metadata
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PIN = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==(?P<version>[^\s;\\]+)")


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def locked_versions(lock: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in lock.read_text(encoding="utf-8").splitlines():
        match = PIN.match(line.strip())
        if match:
            pins[canonical(match.group("name"))] = match.group("version")
    return pins


def installed_versions() -> dict[str, str]:
    return {canonical(dist.metadata["Name"]): dist.version for dist in metadata.distributions()}


def compare(locked: dict[str, str], installed: dict[str, str]) -> list[str]:
    problems = []
    for name, version in sorted(locked.items()):
        actual = installed.get(name)
        if actual is None:
            problems.append(f"{name}: locked {version}, not installed")
        elif actual != version:
            problems.append(f"{name}: locked {version}, installed {actual}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lock", type=Path, default=ROOT / "requirements-prod.lock")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    locked = locked_versions(args.lock)
    problems = compare(locked, installed_versions())
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps({"status": "failed" if problems else "passed", "locked": len(locked),
                        "problems": problems}, indent=2) + "\n",
            encoding="utf-8",
        )
    if not locked:
        print(f"LOCK PARITY FAILED: no pins found in {args.lock}")
        return 1
    if problems:
        print("LOCK PARITY FAILED")
        print("\n".join(f"- {item}" for item in problems))
        return 1
    print(f"LOCK PARITY PASSED ({len(locked)} production pins)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

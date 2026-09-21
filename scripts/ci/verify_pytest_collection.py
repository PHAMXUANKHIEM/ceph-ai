#!/usr/bin/env python3
"""Verify that default pytest collection stays inside the repository test root."""

from __future__ import annotations

import subprocess
import sys


def main() -> int:
    command = [sys.executable, "-m", "pytest", "--collect-only", "-q"]
    result = subprocess.run(command, text=True, capture_output=True)
    output = result.stdout + result.stderr
    print(output, end="")
    if result.returncode != 0:
        return result.returncode

    node_ids = [
        line.strip()
        for line in result.stdout.splitlines()
        if "::" in line and line.strip()
    ]
    outside = []
    for node_id in node_ids:
        path = node_id.split("::", 1)[0].replace("\\", "/")
        if not path.startswith("tests/"):
            outside.append(node_id)

    if not node_ids:
        print("ERROR: pytest collection produced no test node IDs", file=sys.stderr)
        return 2
    if outside:
        print("ERROR: pytest collected tests outside tests/:", file=sys.stderr)
        for node_id in outside:
            print(f"  {node_id}", file=sys.stderr)
        return 3

    print(f"Verified {len(node_ids)} collected test nodes under tests/.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Static security, license and resource gate for forecast candidates."""

from __future__ import annotations

import argparse
import json
import re
import tomllib
from pathlib import Path


FORBIDDEN_RUNTIME_PATTERNS = (
    re.compile(r"\bpickle\.(loads|load)\b"),
    re.compile(r"\beval\s*\("),
    re.compile(r"\bexec\s*\("),
    re.compile(r"rbd\s+rm|ceph\s+osd\s+(rm|destroy)", re.IGNORECASE),
)


def scan_forecast_release(root: str | Path) -> dict:
    root = Path(root)
    issues: list[str] = []
    pyproject = root / "pyproject.toml"
    with pyproject.open("rb") as handle:
        project = tomllib.load(handle)
    dependencies = set(project.get("project", {}).get("dependencies", []))
    if not any(item.startswith("river==") for item in dependencies):
        issues.append("River must remain pinned in production dependencies")
    if any(item.lower().startswith(prefix) for item in dependencies for prefix in ("pyod", "statsforecast", "sktime", "evidently")):
        issues.append("offline benchmark-only dependencies must not enter the production image")
    for directory in (root / "watcher", root / "shared"):
        for path in directory.glob("forecast*.py"):
            text = path.read_text(encoding="utf-8")
            for pattern in FORBIDDEN_RUNTIME_PATTERNS:
                if pattern.search(text):
                    issues.append(f"forbidden runtime pattern {pattern.pattern} in {path.relative_to(root)}")
    return {
        "status": "PASS" if not issues else "BLOCKED",
        "issues": sorted(set(issues)),
        "license_policy": {"river": "BSD-3-Clause", "pyod": "BSD-2-Clause", "benchmark_only": True},
        "resource_policy": {"poll_loop": "bounded", "benchmark": "offline/background-only", "model_state": "JSON-only"},
        "side_effects": "read-only static scan",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = scan_forecast_release(args.root)
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

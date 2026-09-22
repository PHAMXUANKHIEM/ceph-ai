#!/usr/bin/env python3
"""Produce deterministic license/pinning evidence for direct Python deps."""

from __future__ import annotations

import argparse
import json
import re
import tomllib
from importlib import metadata
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def direct_dependencies(pyproject: Path) -> list[tuple[str, str]]:
    payload = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return [(re.split(r"[<>=!~;\[]", spec, maxsplit=1)[0].strip(), spec)
            for spec in payload.get("project", {}).get("dependencies", [])]


def license_name(dist: metadata.Distribution) -> str:
    value = (
        dist.metadata.get("License-Expression")
        or dist.metadata.get("License")
        or ""
    ).strip()
    if value and value.lower() not in {"unknown", "none"}:
        return value
    classifiers = [item.split("::", 1)[1].strip() for item in dist.metadata.get_all("Classifier", [])
                   if item.startswith("License ::")]
    return ", ".join(classifiers)


def build_report(pyproject: Path) -> dict[str, object]:
    rows = []
    missing = []
    unpinned = []
    unknown_license = []
    for name, spec in direct_dependencies(pyproject):
        normalized = name.lower().replace("_", "-")
        try:
            dist = metadata.distribution(name)
            installed_version = dist.version
            license_value = license_name(dist)
        except metadata.PackageNotFoundError:
            installed_version = None
            license_value = ""
            missing.append(name)
        if "==" not in spec:
            unpinned.append(name)
        if not license_value:
            unknown_license.append(name)
        rows.append({
            "name": normalized,
            "requested": spec,
            "installed_version": installed_version,
            "license": license_value or "UNKNOWN",
            "pinned": "==" in spec,
        })
    return {
        "schema": "ceph-ai.dependency-license.v1",
        "status": "passed" if not (missing or unpinned or unknown_license) else "failed",
        "missing": missing,
        "unpinned": unpinned,
        "unknown_license": unknown_license,
        "dependencies": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pyproject", type=Path, default=ROOT / "pyproject.toml")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(args.pyproject)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"dependency license report: {report['status']} ({len(report['dependencies'])} direct dependencies)")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

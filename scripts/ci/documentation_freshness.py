#!/usr/bin/env python3
"""Validate that release-critical documentation is present and reviewed.

The check is intentionally metadata-driven. It does not pretend that a file's
mtime proves operational correctness; an owner must refresh the review date
and expiry in ``docs/documentation-freshness.json``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import date
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
METADATA = ROOT / "docs" / "documentation-freshness.json"


def _git_sha() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _parse_date(value: Any, field: str) -> date:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date") from exc


def check_freshness(
    *, metadata_path: Path = METADATA, as_of: date | None = None,
) -> dict[str, Any]:
    """Return a JSON-safe freshness report and never mutate the repository."""

    report: dict[str, Any] = {
        "schema": "ceph-ai.documentation-freshness-report.v1",
        "as_of": (as_of or date.today()).isoformat(),
        "git_sha": _git_sha(),
        "status": "failed",
        "errors": [],
        "documents": [],
    }
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("metadata must be a JSON object")
        reviewed = _parse_date(payload.get("reviewed_at"), "reviewed_at")
        expires = _parse_date(payload.get("expires_at"), "expires_at")
        today = as_of or date.today()
        if reviewed > today:
            report["errors"].append("reviewed_at is in the future")
        if expires < reviewed:
            report["errors"].append("expires_at is before reviewed_at")
        if today > expires:
            report["errors"].append("documentation review has expired")
        required = payload.get("required_documents")
        if not isinstance(required, list) or not required:
            report["errors"].append("required_documents must be a non-empty list")
            required = []
        for raw_path in required:
            relative = str(raw_path).strip()
            path = ROOT / relative
            exists = bool(relative) and path.is_file()
            report["documents"].append({"path": relative, "exists": exists})
            if not exists:
                report["errors"].append(f"required document is missing: {relative}")
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        report["errors"].append(str(exc))
    report["status"] = "passed" if not report["errors"] else "failed"
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metadata", type=Path, default=METADATA)
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    metadata = args.metadata if args.metadata.is_absolute() else ROOT / args.metadata
    report = check_freshness(metadata_path=metadata, as_of=args.as_of)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = args.output if args.output.is_absolute() else ROOT / args.output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

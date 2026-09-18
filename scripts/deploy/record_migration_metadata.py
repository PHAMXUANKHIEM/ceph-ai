#!/usr/bin/env python3
"""Write a non-secret, atomic migration release artifact."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is required")
    return value


def main() -> None:
    output = Path(
        os.environ.get(
            "MIGRATION_METADATA_PATH",
            "/var/lib/ceph-ai/release-artifacts/migration-latest.json",
        )
    )
    payload = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "environment": _required("MIGRATION_ENVIRONMENT"),
        "git_commit": _required("MIGRATION_GIT_COMMIT"),
        "alembic_head": _required("MIGRATION_HEAD"),
        "revision_before": os.environ.get("MIGRATION_REVISION_BEFORE", ""),
        "revision_after": _required("MIGRATION_REVISION_AFTER"),
        "migration_checksum_sha256": _required("MIGRATION_CHECKSUM_SHA256"),
        "backup_path": _required("MIGRATION_BACKUP_PATH"),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(output.parent, 0o700)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, output)
        os.chmod(output, 0o600)
    finally:
        try:
            Path(temporary).unlink()
        except FileNotFoundError:
            pass
    print(f"Created migration release metadata: {output}")


if __name__ == "__main__":
    main()

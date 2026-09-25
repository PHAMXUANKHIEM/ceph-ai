#!/usr/bin/env python3
"""Create a secret-free release identity manifest.

The manifest is evidence, not production approval.  It binds the source
revision, migration graph, dependency inputs and (when supplied) the image
digest into one reviewable artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEPENDENCY_FILES = (
    Path("pyproject.toml"),
    Path("poetry.lock"),
    Path("requirements.txt"),
    Path("requirements-prod.lock"),
    Path("ceph-health-dashboard/package-lock.json"),
)


def _run(*args: str) -> tuple[int, str]:
    try:
        result = subprocess.run(
            list(args),
            cwd=ROOT,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except OSError as exc:
        return 127, str(exc)
    return result.returncode, result.stdout.strip()


def _git_value(*args: str) -> str:
    rc, output = _run("git", *args)
    return output.splitlines()[0].strip() if rc == 0 and output else ""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def dependency_hashes() -> dict[str, str]:
    return {
        str(path): _sha256(ROOT / path)
        for path in DEPENDENCY_FILES
        if (ROOT / path).is_file()
    }


def migration_heads() -> dict[str, Any]:
    rc, output = _run(sys.executable, "-m", "alembic", "heads")
    heads = [
        line.split()[0]
        for line in output.splitlines()
        if line.strip() and "(head)" in line
    ]
    return {
        "status": "passed" if rc == 0 and len(heads) == 1 else "failed",
        "heads": heads,
        "head_count": len(heads),
        "output": output[-1000:],
    }


def _image_digest(artifacts: Path) -> str:
    configured = os.environ.get("CEPH_AI_IMAGE_DIGEST", "").strip()
    if configured:
        return configured
    registry_refs = sorted(artifacts.rglob("registry-image-ref.txt"))
    if registry_refs:
        reference = registry_refs[0].read_text(encoding="utf-8").strip()
        if "@sha256:" in reference:
            return reference.rsplit("@", 1)[-1]
    candidates = sorted(artifacts.rglob("image-digest.txt"))
    if not candidates:
        return ""
    return candidates[0].read_text(encoding="utf-8").strip()


def _release_evidence(artifacts: Path) -> dict[str, Any]:
    path = artifacts / "release" / "release-evidence.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _documentation_evidence(artifacts: Path) -> dict[str, Any]:
    path = artifacts / "release" / "documentation-freshness.json"
    if not path.is_file():
        return {"status": "missing", "artifact": None}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "invalid", "artifact": str(path.relative_to(ROOT))}
    status = payload.get("status", "invalid") if isinstance(payload, dict) else "invalid"
    try:
        artifact_name = str(path.relative_to(ROOT))
    except ValueError:
        artifact_name = str(path)
    return {
        "status": status if status in {"passed", "failed"} else "invalid",
        "artifact": artifact_name,
        "errors": payload.get("errors", []) if isinstance(payload, dict) else ["invalid report"],
    }


def build_manifest(environment: str, artifacts: Path) -> dict[str, Any]:
    commit = _git_value("rev-parse", "HEAD")
    image_digest = _image_digest(artifacts)
    registry_refs = sorted(artifacts.rglob("registry-image-ref.txt"))
    registry_reference = (
        registry_refs[0].read_text(encoding="utf-8").strip() if registry_refs else ""
    )
    migration = migration_heads()
    release_evidence = _release_evidence(artifacts)
    documentation = _documentation_evidence(artifacts)
    release_status = release_evidence.get("status", {})
    production_decision = release_evidence.get("production_decision", {})
    return {
        "schema": "ceph-ai.release-manifest.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": environment,
        "repository": {
            "commit_sha": commit,
            "branch": _git_value("branch", "--show-current"),
            "worktree_clean": not bool(
                _git_value("status", "--porcelain")
            ),
        },
        "migration": migration,
        "dependencies": {
            "sha256": dependency_hashes(),
            "python": sys.version.split()[0],
        },
        "image": {
            "reference": registry_reference or os.environ.get("CEPH_AI_IMAGE", "").strip() or None,
            "digest": image_digest or None,
            "identity_status": "complete" if image_digest and commit else "incomplete",
        },
        "evidence": {
            "release_evidence": str(
                (artifacts / "release" / "release-evidence.json").relative_to(ROOT)
            )
            if (artifacts / "release" / "release-evidence.json").is_file()
            else None,
            "tests": release_status.get("tests", {"status": "missing"}),
            "quality": release_status.get("quality", {"status": "missing"}),
            "pip_audit": release_status.get("pip_audit", {"status": "missing"}),
            "image_scan": release_status.get("image_scan", {"status": "missing"}),
            "sbom": release_status.get("sbom", {"status": "missing"}),
            "documentation_freshness": documentation,
            "config_fingerprint": os.environ.get("CEPH_AI_CONFIG_FINGERPRINT", "").strip() or None,
            "rollback_artifact": os.environ.get("CEPH_AI_ROLLBACK_ARTIFACT", "").strip() or None,
            "artifact_files": sorted(
                _display_path(path) for path in artifacts.rglob("*") if path.is_file()
            ),
        },
        "production_approval": {
            "status": production_decision.get("status", "pending"),
            "operator_signoff": production_decision.get("operator_signoff", "pending"),
            "staging_rehearsal": production_decision.get("staging_migration_rehearsal", "pending"),
            "rollback_witness": production_decision.get("isolated_live_dr_drill", "pending"),
            "approval_expiry": os.environ.get("CEPH_AI_APPROVAL_EXPIRY", "").strip() or None,
            "residual_risk": os.environ.get(
                "CEPH_AI_RESIDUAL_RISK",
                "Production approval remains pending until staging, rollback and operator witness pass.",
            ).strip(),
        },
    }


def write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
        os.chmod(path, 0o644)
    finally:
        try:
            Path(temporary).unlink()
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment", required=True)
    parser.add_argument("--artifacts", type=Path, default=ROOT / "artifacts")
    parser.add_argument(
        "--output", type=Path, default=ROOT / "artifacts" / "release" / "release-manifest.json"
    )
    args = parser.parse_args()
    artifacts = args.artifacts if args.artifacts.is_absolute() else ROOT / args.artifacts
    output = args.output if args.output.is_absolute() else ROOT / args.output
    write_atomic(output, build_manifest(args.environment, artifacts))
    print(f"Created release manifest: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

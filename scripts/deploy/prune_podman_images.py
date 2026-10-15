#!/usr/bin/env python3
"""Remove unused, untagged Podman images older than seven days.

Preview by default; the systemd timer invokes this with ``--apply``.  The
current and previous approved release images remain protected even when they
are old and have no tag.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import re
# Fixed Podman/systemctl argv, no shell; image IDs and release refs are validated.
import subprocess  # nosec B404
import sys
import time
from pathlib import Path

AGE_SECONDS = 7 * 24 * 60 * 60
ARTIFACTS = Path("/var/lib/ceph-ai/release-artifacts")
LOCK_PATH = Path("/run/ceph-ai/prune-podman-images.lock")
IMAGE_ID = re.compile(r"[a-f0-9]{64}\Z")
IMAGE_REF = re.compile(r"ghcr\.io/[a-z0-9._/-]+@sha256:[a-f0-9]{64}\Z")


def _command(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(  # nosec B603
        args, capture_output=True, text=True, check=False, timeout=120,
    )
    if check and result.returncode:
        raise RuntimeError(f"{args[0]} {args[1]} failed ({result.returncode}): {result.stderr.strip()[:300]}")
    return result


def _unit_state(unit: str) -> str:
    return _command("systemctl", "is-active", unit, check=False).stdout.strip()


def _runtime_ready() -> bool:
    return (_unit_state("ceph-ai-containers.service") == "active"
            and _unit_state("ceph-ai-deploy-request.service") == "inactive")


def _release_refs(artifacts: Path = ARTIFACTS) -> tuple[str, ...]:
    refs = []
    for filename in ("current-image-ref", "previous-image-ref"):
        path = artifacts / filename
        if not path.exists():
            if filename == "current-image-ref":
                raise RuntimeError(f"missing required release reference: {path}")
            continue
        ref = path.read_text(encoding="utf-8").strip()
        if not IMAGE_REF.fullmatch(ref):
            raise RuntimeError(f"invalid release reference: {path}")
        refs.append(ref)
    return tuple(refs)


def _protected_ids(refs: tuple[str, ...]) -> set[str]:
    protected = set()
    for ref in refs:
        image_id = _command("podman", "image", "inspect", ref, "--format", "{{.Id}}").stdout.strip()
        if not IMAGE_ID.fullmatch(image_id):
            raise RuntimeError(f"invalid Podman image ID for release reference: {ref}")
        protected.add(image_id)
    return protected


def _podman_list(*args: str) -> list[dict]:
    data = json.loads(_command("podman", *args, "--format", "json").stdout)
    if not isinstance(data, list):
        raise RuntimeError(f"unexpected Podman response for {' '.join(args)}")
    return data


def candidates(images: list[dict], containers: list[dict], protected: set[str], cutoff: float) -> list[str]:
    """Select only old, untagged image IDs with no container reference."""
    used = {item.get("ImageID") for item in containers}
    selected: dict[str, int] = {}
    for image in images:
        image_id = image.get("Id")
        created = image.get("Created")
        if not IMAGE_ID.fullmatch(image_id or "") or not isinstance(created, int):
            raise RuntimeError("invalid Podman image inventory; refusing cleanup")
        if (created <= cutoff and image.get("Containers") == 0
                and not image.get("RepoTags") and image_id not in used
                and image_id not in protected):
            selected[image_id] = created
    # Remove children before their older parents; never force a removal.
    return sorted(selected, key=lambda image_id: selected[image_id], reverse=True)


def cleanup(*, apply: bool, artifacts: Path = ARTIFACTS, lock_path: Path = LOCK_PATH) -> int:
    with lock_path.open("a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Podman image cleanup is already running")
            return 0
        if not _runtime_ready():
            print("Skipping image cleanup: container stack or deploy is in transition")
            return 0
        refs = _release_refs(artifacts)
        protected = _protected_ids(refs)
        planned = candidates(
            _podman_list("images"), _podman_list("ps", "-a"), protected,
            time.time() - AGE_SECONDS,
        )
        print(f"Eligible unused images older than 7 days: {len(planned)}")
        if not apply:
            for image_id in planned:
                print(image_id)
            return 0
        removed = 0
        failed = 0
        for image_id in planned:
            if not _runtime_ready() or _release_refs(artifacts) != refs:
                print("Stopping cleanup: deployment or release references changed", file=sys.stderr)
                return 2
            result = _command("podman", "image", "rm", "--no-prune", image_id, check=False)
            if result.returncode:
                failed += 1
                print(f"Could not remove {image_id}: {result.stderr.strip()[:300]}", file=sys.stderr)
            else:
                removed += 1
        print(f"Removed {removed} unused images; failures: {failed}")
        return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="delete eligible images instead of previewing")
    args = parser.parse_args()
    try:
        return cleanup(apply=args.apply)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"Podman image cleanup aborted: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

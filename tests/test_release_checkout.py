from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_SCRIPT = REPO_ROOT / "scripts" / "deploy" / "release_checkout.sh"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def release_environment(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    source = tmp_path / "source"
    origin = tmp_path / "origin.git"
    source.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    _git(source, "config", "user.name", "Release Checkout Test")
    _git(source, "config", "user.email", "release-test@example.invalid")
    (source / "release.txt").write_text("first\n", encoding="utf-8")
    _git(source, "add", "release.txt")
    _git(source, "commit", "-m", "first release")
    _git(source, "remote", "add", "origin", str(origin))
    _git(source, "push", "-u", "origin", "main")

    env = os.environ.copy()
    env["CEPH_AI_SOURCE_REPO"] = str(source)
    env["CEPH_AI_RELEASE_ROOT"] = str(tmp_path / "release-root")
    return source, tmp_path / "release-root", env


def _run(command: str, sha: str | None, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    args = ["bash", str(RELEASE_SCRIPT), command]
    if sha:
        args.append(sha)
    return subprocess.run(args, env=env, capture_output=True, text=True)


def test_release_checkout_prepare_activate_and_rollback_are_isolated(release_environment):
    source, root, env = release_environment
    first_sha = _git(source, "rev-parse", "HEAD")

    prepared = _run("prepare", first_sha, env)
    assert prepared.returncode == 0, prepared.stderr
    first_release = root / "releases" / first_sha
    assert _git(first_release, "rev-parse", "HEAD") == first_sha
    assert _git(first_release, "status", "--porcelain", "--untracked-files=all") == ""
    assert not (root / "current").exists()

    activated = _run("activate", first_sha, env)
    assert activated.returncode == 0, activated.stderr
    assert (root / "current").resolve() == first_release
    assert _git(source, "status", "--porcelain", "--untracked-files=all") == ""

    (source / "release.txt").write_text("second\n", encoding="utf-8")
    _git(source, "add", "release.txt")
    _git(source, "commit", "-m", "second release")
    _git(source, "push", "origin", "main")
    second_sha = _git(source, "rev-parse", "HEAD")

    prepared = _run("prepare", second_sha, env)
    assert prepared.returncode == 0, prepared.stderr
    activated = _run("activate", second_sha, env)
    assert activated.returncode == 0, activated.stderr
    assert (root / "current").resolve() == root / "releases" / second_sha
    assert (root / "previous-release").resolve() == first_release

    rolled_back = _run("rollback", None, env)
    assert rolled_back.returncode == 0, rolled_back.stderr
    assert (root / "current").resolve() == first_release
    assert (root / "previous-release").resolve() == root / "releases" / second_sha


def test_release_checkout_refuses_dirty_release(release_environment):
    source, root, env = release_environment
    sha = _git(source, "rev-parse", "HEAD")
    prepared = _run("prepare", sha, env)
    assert prepared.returncode == 0, prepared.stderr
    (root / "releases" / sha / "release.txt").write_text("operator edit\n", encoding="utf-8")

    activated = _run("activate", sha, env)

    assert activated.returncode != 0
    assert "dirty release checkout" in activated.stderr
    assert not (root / "current").exists()

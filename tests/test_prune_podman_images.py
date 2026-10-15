"""The image retention job must never remove a running or rollback image."""

import subprocess
import time

import pytest

from scripts.deploy import prune_podman_images as prune


def _image(char: str, *, created: int, containers: int = 0, tags=None) -> dict:
    return {"Id": char * 64, "Created": created, "Containers": containers, "RepoTags": tags}


def test_candidates_exclude_release_refs_containers_tags_and_recent_images():
    cutoff = int(time.time()) - prune.AGE_SECONDS
    images = [
        _image("a", created=cutoff - 1),
        _image("b", created=cutoff - 2),  # Previous approved release.
        _image("c", created=cutoff - 3),  # Container inventory still uses it.
        _image("d", created=cutoff - 4, containers=1),
        _image("e", created=cutoff - 5, tags=["localhost/ceph-ai:preview"]),
        _image("f", created=cutoff + 1),
    ]
    containers = [{"ImageID": "c" * 64}]

    assert prune.candidates(images, containers, {"b" * 64}, cutoff) == ["a" * 64]


def test_missing_or_malformed_current_release_reference_aborts(tmp_path):
    with pytest.raises(RuntimeError, match="missing required"):
        prune._release_refs(tmp_path)
    (tmp_path / "current-image-ref").write_text("localhost/ceph-ai:latest\n")
    with pytest.raises(RuntimeError, match="invalid release reference"):
        prune._release_refs(tmp_path)


def test_cleanup_skips_when_deploy_is_running(monkeypatch, tmp_path):
    monkeypatch.setattr(prune, "_runtime_ready", lambda: False)
    monkeypatch.setattr(prune, "_podman_list", lambda *_args: pytest.fail("Podman must not be queried"))

    assert prune.cleanup(apply=True, lock_path=tmp_path / "lock") == 0


def test_apply_never_forces_removal_and_stops_if_release_changes(monkeypatch, tmp_path):
    refs = ("ghcr.io/owner/ceph-ai@sha256:" + "1" * 64,)
    commands = []
    checks = iter([True, True, True])
    ref_reads = iter([refs, refs, ("ghcr.io/owner/ceph-ai@sha256:" + "2" * 64,)])
    monkeypatch.setattr(prune, "_runtime_ready", lambda: next(checks))
    monkeypatch.setattr(prune, "_release_refs", lambda _artifacts: next(ref_reads))
    monkeypatch.setattr(prune, "_protected_ids", lambda _refs: {"b" * 64})
    monkeypatch.setattr(prune, "_podman_list", lambda *args: [
        _image("a", created=1), _image("c", created=2), _image("b", created=3),
    ] if args == ("images",) else [])

    def fake_command(*args, **_kwargs):
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(prune, "_command", fake_command)

    assert prune.cleanup(apply=True, lock_path=tmp_path / "lock") == 2
    assert commands == [("podman", "image", "rm", "--no-prune", "c" * 64)]

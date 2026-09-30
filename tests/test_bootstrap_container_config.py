import os

from scripts import bootstrap_container_config as bootstrap


def test_chown_tree_survives_dangling_codex_arg0_symlinks(tmp_path):
    # Codex keeps arg0 alias symlinks under tmp/ that point into the
    # container image, so they dangle on the host. The first immutable
    # deploy failed on exactly this (FileNotFoundError in os.chown).
    alias_dir = tmp_path / "codex" / "tmp" / "arg0" / "codex-arg0abc"
    alias_dir.mkdir(parents=True)
    link = alias_dir / "applypatch"
    link.symlink_to("/opt/codex-packages/standalone/releases/does-not-exist/bin/codex")
    assert not link.exists() and link.is_symlink()

    bootstrap._chown_tree(tmp_path / "codex", uid=str(os.getuid()))

    assert link.is_symlink()
    assert os.lstat(link).st_uid == os.getuid()

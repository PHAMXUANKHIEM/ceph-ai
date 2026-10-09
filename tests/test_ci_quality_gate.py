from pathlib import Path

from scripts.ci.quality_gate import changed_python_files


def test_changed_python_files_excludes_deleted_and_venv_paths(tmp_path: Path):
    present = tmp_path / "dashboard" / "routes.py"
    present.parent.mkdir()
    present.write_text("# current file\n", encoding="utf-8")

    changed = changed_python_files(
        [
            "dashboard/routes.py",
            "dashboard/deleted.py",
            ".venv/lib/python3.12/site-packages/example.py",
            "README.md",
        ],
        root=tmp_path,
    )

    assert changed == ["dashboard/routes.py"]

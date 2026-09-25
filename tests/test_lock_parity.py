import importlib.util
from pathlib import Path


MODULE = Path(__file__).resolve().parents[1] / "scripts" / "ci" / "verify_lock_parity.py"
spec = importlib.util.spec_from_file_location("verify_lock_parity", MODULE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_locked_versions_reads_hashed_pip_compile_output(tmp_path):
    lock = tmp_path / "requirements.lock"
    lock.write_text(
        "# comment\n"
        "Jinja2==3.1.6 \\\n"
        "    --hash=sha256:aa\n"
        "    # via fastapi\n"
        "uvicorn[standard]==0.35.0 ; python_version >= '3.11' \\\n"
        "    --hash=sha256:bb\n",
        encoding="utf-8",
    )
    assert module.locked_versions(lock) == {"jinja2": "3.1.6", "uvicorn": "0.35.0"}


def test_compare_reports_drift_and_missing_packages():
    locked = {"jinja2": "3.1.6", "river": "0.22.0", "numpy": "2.3.0"}
    installed = {"jinja2": "3.1.6", "river": "0.23.0", "pytest": "9.1.1"}
    assert module.compare(locked, installed) == [
        "numpy: locked 2.3.0, not installed",
        "river: locked 0.22.0, installed 0.23.0",
    ]


def test_repository_lock_has_pins():
    assert len(module.locked_versions(module.ROOT / "requirements-prod.lock")) > 20

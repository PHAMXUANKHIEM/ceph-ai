import importlib.util
import json
from pathlib import Path


MODULE = Path(__file__).resolve().parents[1] / "scripts" / "ci" / "coverage_gate.py"
spec = importlib.util.spec_from_file_location("coverage_gate", MODULE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def _cobertura(files: dict[str, list[tuple[int, int, str | None]]]) -> str:
    """files: name -> [(line, hits, condition-coverage or None)]."""
    classes = []
    for name, lines in files.items():
        rows = []
        for number, hits, condition in lines:
            branch = f' branch="true" condition-coverage="{condition}"' if condition else ""
            rows.append(f'<line number="{number}" hits="{hits}"{branch}/>')
        classes.append(f'<class name="{name}" filename="{name}"><lines>{"".join(rows)}</lines></class>')
    return (
        '<?xml version="1.0" ?><coverage><sources><source>.</source></sources>'
        f'<packages><package name="p"><classes>{"".join(classes)}</classes></package></packages></coverage>'
    )


def _baseline(tmp_path, **overrides):
    payload = {
        "critical_target_percent": 90.0,
        "total": {"line_percent": 50.0, "branch_percent": 50.0},
        "critical_paths": {
            "outbox": {"files": ["shared/outbox.py"], "baseline": {"line_percent": 50.0, "branch_percent": 50.0}},
        },
    }
    payload.update(overrides)
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _run(tmp_path, xml: str, baseline: Path, *extra: str) -> tuple[int, dict]:
    report = tmp_path / "coverage.xml"
    report.write_text(xml, encoding="utf-8")
    output = tmp_path / "out" / "coverage-gate.json"
    rc = module.main(["--coverage-xml", str(report), "--baseline", str(baseline), "--output", str(output), *extra])
    return rc, json.loads(output.read_text(encoding="utf-8"))


def test_parse_counts_lines_and_branches(tmp_path):
    path = tmp_path / "c.xml"
    path.write_text(_cobertura({"shared/outbox.py": [(1, 1, None), (2, 0, None), (3, 1, "50% (1/2)")]}))
    counters = module.parse_cobertura(path)["shared/outbox.py"]
    assert counters == {"lines_valid": 3, "lines_covered": 2, "branches_valid": 2, "branches_covered": 1}


def test_gate_passes_at_baseline(tmp_path):
    xml = _cobertura({"shared/outbox.py": [(1, 1, "50% (1/2)"), (2, 0, None)]})
    rc, report = _run(tmp_path, xml, _baseline(tmp_path))
    assert rc == 0
    assert report["status"] == "passed"
    assert report["critical_paths"]["outbox"]["target_met"] is False


def test_gate_fails_on_total_and_critical_regression(tmp_path):
    xml = _cobertura({"shared/outbox.py": [(1, 0, "0% (0/2)"), (2, 1, None), (3, 0, None)]})
    rc, report = _run(tmp_path, xml, _baseline(tmp_path))
    assert rc == 1
    assert any(item.startswith("total line_percent") for item in report["failures"])
    assert any(item.startswith("critical path outbox branch_percent") for item in report["failures"])


def test_missing_critical_file_is_not_treated_as_covered(tmp_path):
    xml = _cobertura({"shared/other.py": [(1, 1, None)]})
    rc, report = _run(tmp_path, xml, _baseline(tmp_path))
    assert rc == 1
    group = report["critical_paths"]["outbox"]
    assert group["missing_files"] == ["shared/outbox.py"]
    assert group["line_percent"] == 0.0
    assert any("unmeasured files" in item for item in report["failures"])


def test_enforced_target_blocks_below_ninety_percent(tmp_path):
    xml = _cobertura({"shared/outbox.py": [(1, 1, "100% (2/2)"), (2, 0, None)]})
    rc, report = _run(tmp_path, xml, _baseline(tmp_path), "--enforce-critical-target")
    assert rc == 1
    assert any("below 90% target" in item for item in report["failures"])


def test_write_baseline_records_current_values(tmp_path):
    baseline = _baseline(tmp_path, total={"line_percent": 99.0, "branch_percent": 99.0})
    xml = _cobertura({"shared/outbox.py": [(1, 1, "50% (1/2)"), (2, 0, None)]})
    rc, _ = _run(tmp_path, xml, baseline, "--write-baseline")
    assert rc == 0
    recorded = json.loads(baseline.read_text(encoding="utf-8"))
    assert recorded["total"]["line_percent"] == 50.0
    assert recorded["critical_paths"]["outbox"]["baseline"]["branch_percent"] == 50.0
    assert recorded["recorded_at"]


def test_write_baseline_applies_margin(tmp_path):
    baseline = _baseline(tmp_path)
    xml = _cobertura({"shared/outbox.py": [(1, 1, "50% (1/2)"), (2, 0, None)]})
    rc, _ = _run(tmp_path, xml, baseline, "--write-baseline", "--margin", "1.5")
    assert rc == 0
    recorded = json.loads(baseline.read_text(encoding="utf-8"))
    assert recorded["margin_percent"] == 1.5
    assert recorded["total"]["line_percent"] == 48.5
    assert recorded["total"]["measured_line_percent"] == 50.0


def test_skipped_tests_are_reported(tmp_path):
    junit = tmp_path / "pytest.xml"
    junit.write_text(
        '<testsuite><testcase classname="tests.test_outbox" name="test_a"><skipped/></testcase>'
        '<testcase classname="tests.test_outbox" name="test_b"/></testsuite>',
        encoding="utf-8",
    )
    xml = _cobertura({"shared/outbox.py": [(1, 1, "50% (1/2)"), (2, 0, None)]})
    rc, report = _run(tmp_path, xml, _baseline(tmp_path), "--junit", str(junit))
    assert rc == 0
    assert report["skipped_tests"] == 1
    assert report["skipped_test_ids"] == ["tests.test_outbox::test_a"]

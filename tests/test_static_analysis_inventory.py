import importlib.util
import json
from pathlib import Path


MODULE = Path(__file__).resolve().parents[1] / "scripts" / "ci" / "static_analysis_inventory.py"
spec = importlib.util.spec_from_file_location("static_analysis_inventory", MODULE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_owner_and_tier_follow_burn_down_priority():
    assert module.owner_for("worker/executor/commands.py") == "executor"
    assert module.owner_for("scripts/deploy/redact_deploy_output.py") == "deploy"
    assert module.owner_for("dashboard/routes/volumes.py") == "dashboard"
    assert module.tier_for("worker/main.py") == "critical"
    assert module.tier_for("shared/single_full_policy.py") == "critical"
    assert module.tier_for("dashboard/routes/volumes.py") == "standard"


def test_parsers_normalise_paths_and_rules(tmp_path):
    ruff = json.dumps([{"code": "F821", "filename": str(tmp_path / "worker/main.py"),
                        "location": {"row": 3}, "message": "Undefined name `x`"}])
    assert module.parse_ruff(ruff, tmp_path, "ruff") == [{
        "tool": "ruff", "rule": "F821", "file": "worker/main.py", "line": 3, "message": "Undefined name `x`",
    }]
    mypy = (
        "shared/db.py:10: error: Incompatible return value type  [return-value]\n"
        "shared/db.py:11:5: error: Missing annotation\n"
        "shared/db.py:12: note: ignored\n"
    )
    parsed = module.parse_mypy(mypy, tmp_path)
    assert [(item["rule"], item["line"]) for item in parsed] == [("return-value", 10), ("error", 11)]
    bandit = json.dumps({"results": [{"test_id": "B324", "filename": "./watcher/a.py", "line_number": 7,
                                      "issue_severity": "HIGH", "issue_text": "weak hash"}]})
    assert module.parse_bandit(bandit, tmp_path)[0]["file"] == "watcher/a.py"


def _finding(tool, file, severity=None):
    item = {"tool": tool, "rule": "X", "file": file, "owner": module.owner_for(file), "tier": module.tier_for(file)}
    if severity:
        item["severity"] = severity
    return item


def test_budget_blocks_growth_in_total_and_critical_tier():
    findings = [
        _finding("ruff", "worker/main.py"),
        _finding("ruff", "dashboard/routes/volumes.py"),
        _finding("bandit", "worker/main.py", "HIGH"),
    ]
    counts = module.count(findings)
    assert counts["ruff"] == {"total": 2, "critical": 1}
    assert counts["bandit_high"] == {"total": 1, "critical": 1}
    budget = {"max": {"ruff": {"total": 2, "critical": 0}, "bandit_high": {"total": 0, "critical": 0}}}
    failures = module.compare(counts, budget)
    assert "ruff critical findings 1 > budget 0" in failures
    assert "bandit_high total findings 1 > budget 0" in failures
    assert not any(item.startswith("ruff total") for item in failures)


def test_main_writes_reports_even_when_budget_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "scan", lambda root: [_finding("mypy", "worker/main.py")])
    budget = tmp_path / "budget.json"
    budget.write_text(json.dumps({"max": {"mypy": {"total": 0, "critical": 0}}}), encoding="utf-8")
    output = tmp_path / "out"
    assert module.main(["--budget", str(budget), "--output", str(output)]) == 1
    report = json.loads((output / "static-analysis-inventory.json").read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["before"]["mypy"]["total"] == 0
    assert report["after"]["mypy"]["total"] == 1
    assert report["by_owner"] == {"worker:mypy": 1}

    assert module.main(["--budget", str(budget), "--output", str(output), "--write-budget"]) == 0
    assert json.loads(budget.read_text(encoding="utf-8"))["max"]["mypy"] == {"total": 1, "critical": 1}

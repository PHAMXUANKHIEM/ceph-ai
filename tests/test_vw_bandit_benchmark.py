import ast
from pathlib import Path


def test_vw_benchmark_is_evaluation_only():
    source = (Path(__file__).resolve().parents[1] / "scripts" / "benchmark_vw_bandit.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    top_level_imports = {
        alias.name.split(".")[0]
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "vowpalwabbit" not in top_level_imports
    assert "paramiko" not in top_level_imports

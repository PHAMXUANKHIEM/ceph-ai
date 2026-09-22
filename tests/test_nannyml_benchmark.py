import ast
from pathlib import Path


def test_nannyml_benchmark_isolated_from_production_imports():
    source = (Path(__file__).resolve().parents[1] / "scripts" / "benchmark_nannyml.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    top_level_imports = {
        alias.name.split(".")[0]
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "nannyml" not in top_level_imports
    assert "shared" not in top_level_imports

"""Slow Ceph/SSH calls never run on the dashboard's event loop (08/10/2026).

The dashboard runs one uvicorn process. Volumes routes called ceph_client
over SSH inside ``async def`` handlers, so one 20-160 s card froze every other
page (Object Storage included) for every user.
"""

import ast
from pathlib import Path

from dashboard.routes import volumes
from shared import ceph_query_cache

ROOT = Path(__file__).resolve().parents[1]
SLOW = ("query_", "fetch_", "_cached_", "discover_", "_load_", "run_ceph")


def _blocking_calls_on_the_event_loop(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        parents = {child: parent for parent in ast.walk(node) for child in ast.iter_child_nodes(parent)}
        threaded = {id(arg) for call in ast.walk(node) if isinstance(call, ast.Call)
                    and getattr(call.func, "attr", "") in ("to_thread", "run_in_executor") for arg in call.args}
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            name = getattr(call.func, "attr", None) or getattr(call.func, "id", "") or ""
            parent = parents.get(call)
            nested = False
            while parent is not None and parent is not node:
                if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    nested = True
                    break
                parent = parents.get(parent)
            if name.startswith(SLOW) and id(call.func) not in threaded and not nested:
                found.append(f"{path.name}:{call.lineno} {node.name} -> {name}")
    return found


def test_no_async_route_calls_ceph_on_the_event_loop():
    offenders = []
    for path in sorted((ROOT / "dashboard" / "routes").glob("*.py")):
        offenders += _blocking_calls_on_the_event_loop(path)
    assert offenders == []


def test_read_only_insights_are_served_from_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(ceph_query_cache, "_cache_dir", tmp_path)
    monkeypatch.setattr(ceph_query_cache, "_memory", {})
    calls = []

    def query_rbd_pool_overview(pool):
        calls.append(pool)
        return {"pool": pool, "size": 3}

    cluster = type("Cluster", (), {"id": "c1", "is_default": True})()

    first = volumes._insight(cluster, query_rbd_pool_overview, None, "volumes")
    second = volumes._insight(cluster, query_rbd_pool_overview, None, "volumes")

    assert first == second == {"pool": "volumes", "size": 3} and calls == ["volumes"]

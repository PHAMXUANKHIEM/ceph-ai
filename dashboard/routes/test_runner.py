import json
import logging

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from dashboard.routes.auth import require_login
from shared import db
from shared.cluster_nodes import configured_nodes as _configured_nodes
from shared.models import TestRunnerConfig
from shared import test_runner_baselines as baselines
from worker.executor.ssh_executor import test_all_nodes

logger = logging.getLogger(__name__)

router = APIRouter()

# Epic 10 (Ceph Upgrade Test Runner) placeholder. This router intentionally
# has no test-case business logic yet -- 63 upgrade test cases, SSH
# execution via worker/executor/ssh_executor.py's new execute_with_retry/
# execute_background, the live SSE/WS log stream, and Markdown report
# export are future Epic 10 stories. This stub only proves the wiring: the
# ceph-upgrade-test-runner-frontend React app (relocated from the
# throwaway prototype, see its vite.config.js /api proxy) talks to THIS
# same dashboard/app.py FastAPI backend rather than a second standalone
# process, consistent with Epic 10's "one shared backend" decision.
#
# Deliberately no `Depends(require_login)` on this one endpoint -- it is a
# plain liveness probe (mirrors a typical /health endpoint), not a data or
# action route; every real Epic 10 endpoint added on top of this router
# later must gate on require_login like every other API route in
# dashboard/routes/ (see nodes.py, upgrade.py).
@router.get("/api/test-runner/health")
async def test_runner_health():
    return {"status": "ok"}


# BASELINE_FILE_KEYS/BASELINE_FILES_DIR now live in shared/test_runner_baselines.py
# (Story 10.4) so worker/executor/test_runner/group_b.py can read the same
# uploaded files without worker/ importing from dashboard/. Referenced below
# as `baselines.BASELINE_FILES_DIR` (qualified, via `import ... as baselines`)
# rather than `from shared.test_runner_baselines import BASELINE_FILES_DIR` --
# the latter would bind a SECOND, independent copy of the name in this
# module's own namespace, so a test that monkeypatches
# shared.test_runner_baselines.BASELINE_FILES_DIR (the one true source
# group_b.py's baseline_file_path()/read_baseline_text() actually read)
# would silently NOT affect this route file's own reads, and vice versa.
# Qualified access keeps exactly one binding, patchable from either side.
# Stored on disk keyed by the fixed key name (not the client's original
# filename) specifically to avoid a client-supplied filename being used to
# build a filesystem path (path-injection risk) -- see upload_baseline_file()
# below.


def _get_or_create_config(session) -> TestRunnerConfig:
    """Singleton-row lookup -- same "1 cluster at a time" posture as
    config/settings.py. Creates the row on first write (POST /config or a
    baseline upload) rather than requiring a separate init step."""
    config = session.query(TestRunnerConfig).first()
    if config is None:
        config = TestRunnerConfig()
        session.add(config)
        session.flush()
    return config


def _baseline_files_dict(config: TestRunnerConfig | None) -> dict:
    if config is None or not config.baseline_files:
        return {}
    try:
        return json.loads(config.baseline_files)
    except (TypeError, ValueError):
        return {}


def _config_response(config: TestRunnerConfig | None) -> dict:
    """Shared shape for GET /config -- also used to build the response
    right after a POST /config or baseline upload so callers get a fresh
    reflect-back without a second round trip."""
    baseline_present = _baseline_files_dict(config)
    return {
        "nodes": _configured_nodes(),
        "rgw_endpoint_zone_a": config.rgw_endpoint_zone_a if config else None,
        "rgw_endpoint_zone_b": config.rgw_endpoint_zone_b if config else None,
        "rgw_endpoint_vip": config.rgw_endpoint_vip if config else None,
        "client_host": config.client_host if config else None,
        "test_groups": json.loads(config.test_groups) if config and config.test_groups else [],
        "priorities": json.loads(config.priorities) if config and config.priorities else [],
        "baseline_files": {
            key: (key in baseline_present) for key in baselines.BASELINE_FILE_KEYS
        },
    }


@router.get("/api/test-runner/config")
async def get_test_runner_config(user: str = Depends(require_login)):
    with db.SessionLocal() as session:
        config = session.query(TestRunnerConfig).first()
        return JSONResponse(_config_response(config))


@router.post("/api/test-runner/config")
async def save_test_runner_config(request: Request, user: str = Depends(require_login)):
    """Upserts RGW endpoints + test-group/priority selection onto the
    singleton row. All fields optional -- no minimum enforced this story
    (see spec's I/O matrix). An explicit empty list for test_groups/
    priorities is preserved as-is (not silently defaulted back to "all
    selected") -- that's why these are only touched when the key is
    present in the payload at all, using None as "key absent". Same
    `await request.json()` JSON-body pattern other API routes in this
    codebase already use (see nodes.py, backups.py, deploy_cluster.py)."""
    payload = await request.json()
    with db.SessionLocal() as session:
        config = _get_or_create_config(session)
        if "rgw_endpoint_zone_a" in payload:
            config.rgw_endpoint_zone_a = payload.get("rgw_endpoint_zone_a") or None
        if "rgw_endpoint_zone_b" in payload:
            config.rgw_endpoint_zone_b = payload.get("rgw_endpoint_zone_b") or None
        if "rgw_endpoint_vip" in payload:
            config.rgw_endpoint_vip = payload.get("rgw_endpoint_vip") or None
        if "client_host" in payload:
            config.client_host = payload.get("client_host") or None
        if "test_groups" in payload:
            config.test_groups = json.dumps(payload.get("test_groups") or [])
        if "priorities" in payload:
            config.priorities = json.dumps(payload.get("priorities") or [])
        session.commit()
        session.refresh(config)
        return JSONResponse(_config_response(config))


@router.post("/api/test-runner/config/baseline/{baseline_key}")
async def upload_baseline_file(
    baseline_key: str, file: UploadFile = File(...), user: str = Depends(require_login)
):
    if baseline_key not in baselines.BASELINE_FILE_KEYS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"baseline_key không hợp lệ: {baseline_key!r} -- chỉ chấp nhận "
                f"{', '.join(baselines.BASELINE_FILE_KEYS)}."
            ),
        )

    baselines.BASELINE_FILES_DIR.mkdir(parents=True, exist_ok=True)
    # Saved under the fixed baseline_key, never the client-supplied
    # filename (file.filename) -- avoids path-injection risk from a
    # client-controlled name reaching the filesystem.
    dest_path = baselines.BASELINE_FILES_DIR / baseline_key
    raw_bytes = await file.read()
    dest_path.write_bytes(raw_bytes)

    with db.SessionLocal() as session:
        config = _get_or_create_config(session)
        baseline_present = _baseline_files_dict(config)
        # Stored as "<baseline_files_dir_name>/<key>" rather than an
        # absolute path or a path computed against PROJECT_ROOT -- keeps
        # this independent of exactly where BASELINE_FILES_DIR resolves to
        # (tests point it at a tmp dir).
        baseline_present[baseline_key] = f"{baselines.BASELINE_FILES_DIR.name}/{baseline_key}"
        config.baseline_files = json.dumps(baseline_present)
        session.commit()
        session.refresh(config)
        return JSONResponse(_config_response(config))


@router.post("/api/test-runner/ssh-check")
async def ssh_check(user: str = Depends(require_login)):
    """Live SSH connectivity check against every node Settings has
    configured -- wired to Story 10.1's test_all_nodes(), which already
    guarantees dict[host, bool] and never raises for a per-host failure.
    Zero configured nodes -> test_all_nodes([]) -> {} (no special-casing
    needed here; the frontend renders the "chưa cấu hình node" hint off an
    empty result)."""
    hosts = [node["host"] for node in _configured_nodes()]
    results = test_all_nodes(hosts)
    return JSONResponse(results)

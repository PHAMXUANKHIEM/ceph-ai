import logging

from fastapi import APIRouter

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

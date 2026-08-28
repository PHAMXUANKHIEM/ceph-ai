from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared.ai_cost import summary

router = APIRouter()
templates = make_templates()


def _hours(value: str | None) -> int:
    try:
        return max(1, min(int(value or 24), 8760))
    except (TypeError, ValueError):
        return 24


@router.get("/api/ai-cost")
async def ai_cost_api(hours: str | None = None, _user: str = Depends(require_login)):
    return summary(_hours(hours))


@router.get("/ai-cost", response_class=HTMLResponse)
async def ai_cost_page(request: Request, hours: str | None = None, user: str = Depends(require_login)):
    data = summary(_hours(hours))
    return templates.TemplateResponse(request, "ai_cost.html", {"user": user, **data})

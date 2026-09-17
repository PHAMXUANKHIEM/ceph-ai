from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared.ai_cost import summary

router = APIRouter()
templates = make_templates()
DETAIL_PAGE_SIZE = 10


def _hours(value: str | None) -> int:
    try:
        return max(1, min(int(value or 24), 8760))
    except (TypeError, ValueError):
        return 24


def _page(value: str | None) -> int:
    try:
        return max(1, int(value or 1))
    except (TypeError, ValueError):
        return 1


@router.get("/api/ai-cost")
async def ai_cost_api(hours: str | None = None, _user: str = Depends(require_login)):
    return summary(_hours(hours))


@router.get("/ai-cost", response_class=HTMLResponse)
async def ai_cost_page(
    request: Request,
    hours: str | None = None,
    page: str | None = None,
    page_size: str | None = None,
    user: str = Depends(require_login),
):
    data = summary(_hours(hours))
    all_groups = data["groups"]
    # Pagination is intentionally fixed across the dashboard. Keep accepting
    # the legacy query parameter for old links, but never let it alter layout.
    size = DETAIL_PAGE_SIZE
    page_count = max(1, (len(all_groups) + size - 1) // size)
    current_page = min(_page(page), page_count)
    start = (current_page - 1) * size
    data.update({
        "groups": all_groups[start:start + size],
        "group_count": len(all_groups),
        "page": current_page,
        "page_size": size,
        "page_count": page_count,
    })
    return templates.TemplateResponse(request, "ai_cost.html", {"user": user, **data})

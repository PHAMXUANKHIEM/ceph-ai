import time
from pathlib import Path

from fastapi.templating import Jinja2Templates

from dashboard.vntime import format_vn

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

# Computed once at import time — i.e. once per Dashboard process, not once
# per request — and appended as a `?v=` query string on every static asset
# URL in every template (see templates/*.html's `?v={{ static_version }}`).
# StaticFiles sends no Cache-Control header (only Last-Modified/ETag), so a
# browser that cached an older build's static/*.js|css under the SAME URL
# can keep serving it indefinitely without a way to force a refetch — this
# is exactly what made a Dashboard restart alone not enough to pick up a
# chat_widget.js fix in practice (the HTML re-rendered fine; the script tag
# still pointed at a URL the browser already had a stale copy of). Changing
# this query string on every process restart makes it a NEW url each time,
# which a browser always fetches fresh; within one running process's
# lifetime, assets still cache normally (same version -> same URL).
STATIC_VERSION = str(int(time.time()))


def make_templates() -> Jinja2Templates:
    """Every route module that renders a template should get its
    Jinja2Templates instance from here (not construct one directly) so
    `static_version` is available in all of them uniformly — see
    dashboard/routes/*.py."""
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.globals["static_version"] = STATIC_VERSION
    # `{{ some_utc_datetime|vntime }}` — every *_at column is stored naive
    # UTC (shared/models.py); this renders it as Vietnam local time instead
    # of raw UTC digits (see dashboard/vntime.py for why).
    templates.env.filters["vntime"] = format_vn
    return templates

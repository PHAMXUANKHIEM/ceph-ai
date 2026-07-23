"""Vietnam-time display helpers.

Every datetime in this codebase is stored as naive UTC
(`datetime.utcnow()` — see shared/models.py's `default=` throughout).
Converting to Asia/Ho_Chi_Minh (UTC+7, no DST) happens ONLY here, at the
two display boundaries (Jinja templates via `format_vn`, JSON API
responses consumed by frontend JS via `to_utc_iso`) — storage, ordering,
and every other piece of business logic elsewhere stays UTC, untouched.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def _as_utc(value: datetime) -> datetime:
    """Every datetime column in this DB is naive (no tzinfo) but represents
    UTC — attach that explicitly before converting. A naive datetime's
    `.astimezone()` would otherwise assume it's already local to the
    SERVER process's OS timezone, which this deployment does not guarantee
    is UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def to_utc_iso(value: datetime) -> str:
    """For JSON API responses consumed by frontend JS. Appends an explicit
    'Z' UTC marker so `new Date(iso)` on the frontend parses it as UTC —
    per the ECMAScript spec, a timezone-less ISO string is otherwise parsed
    as already being in the BROWSER's own local timezone, which silently
    made every JS-rendered timestamp on this dashboard off by however many
    hours that browser's UTC offset was (7, for a Vietnam-based operator
    whose browser is actually set to their real local timezone)."""
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def format_vn(value: datetime | None) -> str:
    """Jinja filter (registered as `vntime` in dashboard/templating.py) —
    renders a stored UTC datetime as Vietnam local time, `dd/mm/YYYY
    HH:MM:SS`, the format operators actually expect to see."""
    if value is None:
        return "—"
    return _as_utc(value).astimezone(VN_TZ).strftime("%d/%m/%Y %H:%M:%S")

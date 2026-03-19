"""Timezone support for KoServer templates."""
from datetime import datetime, timezone as dt_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_current_tz: str = "UTC"

COMMON_TIMEZONES = [
    "UTC",
    "Europe/London",
    "Europe/Paris",
    "Europe/Berlin",
    "Europe/Athens",
    "Europe/Moscow",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Sao_Paulo",
    "Asia/Dubai",
    "Asia/Kolkata",
    "Asia/Bangkok",
    "Asia/Singapore",
    "Asia/Tokyo",
    "Asia/Seoul",
    "Asia/Shanghai",
    "Australia/Sydney",
    "Pacific/Auckland",
]


def get_current_tz() -> str:
    return _current_tz


def set_current_tz(tz_name: str) -> None:
    global _current_tz
    try:
        ZoneInfo(tz_name)
        _current_tz = tz_name
    except (ZoneInfoNotFoundError, KeyError):
        pass


def mins_hm(minutes: float) -> str:
    """Format a duration in minutes as 'Xh Ym' (e.g. 90.5 → '1h 30m')."""
    total = max(0, int(round(minutes)))
    h, m = divmod(total, 60)
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def localtime_filter(value: str) -> str:
    """Jinja2 filter: convert a UTC datetime string to the active timezone."""
    if not value:
        return value
    try:
        tz = ZoneInfo(_current_tz)
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                dt = datetime.strptime(value[:19], fmt).replace(tzinfo=dt_timezone.utc)
                return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                continue
        return value  # already a bare date or unknown format
    except Exception:
        return value

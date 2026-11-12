"""Hour of day without platform surprises.

``datetime.fromtimestamp`` raises on Windows for instants before 1970 in local
time, which simulated data near zero can hit. This is plain arithmetic on the
local UTC offset (daylight-saving changes inside a few days shift the answer by
at most an hour, which is fine for "what time of day is it").
"""

from __future__ import annotations

from datetime import datetime

_OFFSET_S = int(datetime.now().astimezone().utcoffset().total_seconds())


def hour_of_day(ts: float, offset_s: int = _OFFSET_S) -> int:
    return int(((ts + offset_s) // 3600) % 24)


def day_of(ts: float, offset_s: int = _OFFSET_S) -> int:
    """A number that changes at local midnight (days since 1970, local)."""
    return int((ts + offset_s) // 86400)


def format_duration(seconds: float) -> str:
    """"45 s", "7 min", "2 h 14 min": short and the same in every language."""
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{round(seconds)} s"
    minutes = round(seconds / 60)
    if minutes < 90:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"

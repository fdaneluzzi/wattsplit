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

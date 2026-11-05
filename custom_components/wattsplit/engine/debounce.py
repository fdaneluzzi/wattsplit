"""Confirming state changes before believing them.

Some integrations flap: an entity reads off, on, off within seconds (a cloud
poll, a controller re-sending its state). Treating each flap as a real switch
pollutes the on/off timeline and the learning windows. A change is only
confirmed once it has held for ``delay_s``; a change that is undone before that
never happened. The confirmed change keeps its original timestamp.
"""

from __future__ import annotations


class StateDebouncer:
    def __init__(self, delay_s: float = 20.0) -> None:
        self._delay_s = delay_s
        self._applied: dict[str, bool] = {}
        self._pending: dict[str, tuple[float, bool]] = {}

    def sync(self, key: str, is_on: bool) -> None:
        """Set the confirmed state without a transition (startup, reconnect)."""
        self._applied[key] = is_on
        self._pending.pop(key, None)

    def submit(self, key: str, ts: float, is_on: bool) -> None:
        """A raw state change was seen at ``ts``."""
        if key not in self._applied:
            self._applied[key] = is_on
            return
        if is_on == self._applied[key]:
            self._pending.pop(key, None)  # it went back: the flap is cancelled
        else:
            self._pending[key] = (ts, is_on)

    def poll(self, now: float) -> list[tuple[str, float, bool]]:
        """Changes that have held long enough: ``(key, original_ts, is_on)``."""
        confirmed = []
        for key, (ts, is_on) in list(self._pending.items()):
            if now - ts >= self._delay_s:
                confirmed.append((key, ts, is_on))
                self._applied[key] = is_on
                del self._pending[key]
        return confirmed

"""Re-run the engine over a logged history."""

from __future__ import annotations

from .disaggregator import Disaggregator
from .models import DeviceSpec
from .store import EdgeLog


def replay(
    log: EdgeLog, specs: list[DeviceSpec], since: float = 0.0, **kwargs
) -> Disaggregator:
    """Build a fresh :class:`Disaggregator` and feed it the log in live order.

    State events are applied at their own timestamp, edges at the moment the
    live detector reported them (``seen_ts``). Ties go to the state event, as
    they do live. Extra keyword arguments are passed to ``Disaggregator`` so
    callers can try a different ``match_window_s`` and compare outcomes.
    """
    diss = Disaggregator(specs, **kwargs)
    timeline: list[tuple[float, int, object]] = [
        (ts, 0, (dev, on)) for ts, dev, on in log.state_events(since)
    ]
    timeline += [(e.seen_ts or e.ts, 1, e) for e, _ in log.edges(since)]
    for ts, kind, payload in sorted(timeline, key=lambda x: (x[0], x[1])):
        if kind == 0:
            dev, on = payload
            diss.on_state(dev, on, ts)
        else:
            diss.on_edge(payload)
    return diss

"""Re-run the engine over a logged history."""

from __future__ import annotations

from .disaggregator import Disaggregator
from .models import DeviceSpec
from .store import EdgeLog


def replay(
    log: EdgeLog,
    specs: list[DeviceSpec],
    since: float = 0.0,
    restricted_channels: frozenset[str] = frozenset(),
    **kwargs,
) -> Disaggregator:
    """Build a fresh :class:`Disaggregator` and feed it the log in live order.

    State events are applied at their own timestamp; toggles and edges at the
    moment the live engine produced them (``seen_ts``). Ties go state event,
    then toggle, then edge, as they do live. Extra keyword arguments are passed
    to ``Disaggregator`` so callers can try a different ``match_window_s`` and
    compare outcomes. Edges seen on ``restricted_channels`` (evidence-only
    sensors) never enter the discovery inbox.
    """
    diss = Disaggregator(specs, track_toggles=False, **kwargs)
    timeline: list[tuple[float, int, object]] = [
        (ts, 0, (dev, on)) for ts, dev, on in log.state_events(since)
    ]
    timeline += [(t.seen_ts, 1, t) for t in log.toggles(since)]
    timeline += [(ts, 3, (dev, value)) for ts, dev, value in log.counters(since)]
    timeline += [(e.seen_ts or e.ts, 2, e) for e, _ in log.edges(since)]
    for ts, kind, payload in sorted(timeline, key=lambda x: (x[0], x[1])):
        if kind == 0:
            dev, on = payload
            diss.on_state(dev, on, ts)
        elif kind == 1:
            diss.apply_toggle(payload)
        elif kind == 3:
            dev, value = payload
            diss.on_counter(dev, ts, value)
        else:
            channels = {c for c, _ in payload.parts}
            diss.on_edge(payload, discover=not (channels and channels <= restricted_channels))
    return diss

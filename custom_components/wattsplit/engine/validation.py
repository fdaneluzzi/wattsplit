"""Scoring the engine against appliances that have a real power sensor.

An appliance with both an on/off entity and a power sensor is ground truth. The
engine is run on it a second time *as if the power sensor did not exist*: a
shadow device learns its power from the whole-house channels around its state
changes, exactly like an unmetered appliance would, while a second shadow
measures the same thing from the real sensor. Comparing the two gives an
accuracy figure that comes from the user's own house, not from a benchmark.

Only the channel-means evidence is scored (the shadow sees no step edges).
"""

from __future__ import annotations

from .disaggregator import Disaggregator
from .models import DeviceSpec

TRUTH = "truth"


class Validator:
    def __init__(self, device_id: str, name: str = "") -> None:
        spec = DeviceSpec(device_id, name or device_id, has_state=True)
        self.device_id = device_id
        self._estimate = Disaggregator([spec])
        self._truth = Disaggregator([spec])

    def set_timing(
        self,
        before_s: float,
        after_s: float,
        skip_s: float,
        min_window_s: float,
        channel: str | None = None,
    ) -> None:
        """Windows for one house channel (the truth sensor keeps the defaults)."""
        self._estimate.set_timing(before_s, after_s, skip_s, min_window_s, channel=channel)

    def sync_state(self, is_on: bool) -> None:
        self._estimate.sync_state(self.device_id, is_on)
        self._truth.sync_state(self.device_id, is_on)

    def on_state(self, is_on: bool, ts: float) -> None:
        self._estimate.on_state(self.device_id, is_on, ts)
        self._truth.on_state(self.device_id, is_on, ts)

    def on_sample(
        self, ts: float, channels: dict[str, float], truth_w: float | None
    ) -> None:
        """``channels`` must still contain the appliance's own consumption."""
        self._estimate.on_sample(ts, channels)
        if truth_w is not None:
            self._truth.on_sample(ts, {TRUTH: truth_w})

    def report(self) -> dict:
        est = self._estimate.state_device(self.device_id)
        truth = self._truth.state_device(self.device_id)
        assert est is not None and truth is not None
        estimate_w, truth_w = est.learned_w, truth.learned_w
        error_pct = None
        if estimate_w is not None and truth_w:
            error_pct = round(100.0 * (estimate_w - truth_w) / truth_w, 1)
        return {
            "estimate_w": None if estimate_w is None else round(estimate_w, 1),
            "truth_w": None if truth_w is None else round(truth_w, 1),
            "error_pct": error_pct,
            "estimate_confidence": round(est.confidence, 2),
            "truth_confidence": round(truth.confidence, 2),
            "toggles": est.describe()["toggles"],
            "channels": dict(est.channel_powers),
        }

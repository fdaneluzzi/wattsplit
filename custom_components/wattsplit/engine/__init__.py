"""WattSplit inference engine.

Pure Python, no Home Assistant imports, so it can be tested in isolation and
later published as its own package.
"""

from .baseload import BaseLoad
from .composer import house_power_w
from .counter import CounterEstimator
from .debounce import StateDebouncer
from .detector import GlitchFilter, MultiChannelDetector, StepDetector
from .disaggregator import Disaggregator
from .discovery import Candidate, Discoverer
from .models import DeviceSpec, Edge, Estimate, PowerMode, Toggle
from .plan import Plan, Sources, channel_values, choose_plan, consumption_w
from .replay import replay
from .snooze import Snoozes
from .store import EdgeLog
from .timing import RateTracker, Timing, timing_for
from .validation import Validator

__all__ = [
    "Snoozes",
    "BaseLoad",
    "Candidate",
    "CounterEstimator",
    "DeviceSpec",
    "Disaggregator",
    "Discoverer",
    "Edge",
    "EdgeLog",
    "Estimate",
    "GlitchFilter",
    "MultiChannelDetector",
    "Plan",
    "PowerMode",
    "RateTracker",
    "Sources",
    "StateDebouncer",
    "StepDetector",
    "Timing",
    "Toggle",
    "Validator",
    "channel_values",
    "choose_plan",
    "consumption_w",
    "house_power_w",
    "replay",
    "timing_for",
]

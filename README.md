# WattSplit

Split your house meter into per-appliance virtual sub-meters in Home Assistant:
power and on/off detection inferred from your whole-house power signal. Works for devices Home Assistant has never heard of
(an electric shower, a dishwasher) and for devices that report on/off but have
no power meter (many air conditioners).

> **Status: M0 — engine core.** The inference engine (step detection, power
> modes, discovery of unknown appliances, SQLite edge log, replay) and its tests
> exist; the Home Assistant integration shell (config flow, entities, panel) is
> next. Not installable yet. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## How it works, in one paragraph

Every appliance leaves a fingerprint in the house meter: a 5 500 W shower is a
clean +5 500 W step when it starts and −5 500 W when it stops. WattSplit
watches for those steps, matches them against the power you told it each device
draws, and for devices that already have an on/off entity it learns the missing
power from the steps that line up with their state changes. Everything is
local, explainable, and reports a confidence.

## Development

```bash
python -m pytest
```

The engine (`custom_components/wattsplit/engine/`) has no Home Assistant
dependency; tests run against a synthetic-house simulator in `tests/sim.py`.

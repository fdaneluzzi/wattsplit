# WattSplit

Split your house meter into per-appliance virtual sub-meters in Home Assistant:
power and on/off detection inferred from your whole-house power signal. Works for devices Home Assistant has never heard of
(an electric shower, a dishwasher) and for devices that report on/off but have
no power meter (many air conditioners).

> **Status: M1 — Home Assistant shell, early.** The inference engine (step
> detection, power modes, discovery of unknown appliances, SQLite edge log,
> replay) and the integration shell (config flow, appliance subentries,
> entities, actions) exist and load on Home Assistant 2026.10.4. Learning from
> real appliance toggles is still being validated, and the web panel is next.
> Not published yet. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## How it works, in one paragraph

Every appliance leaves a fingerprint in the house meter: a 5 500 W shower is a
clean +5 500 W step when it starts and −5 500 W when it stops. WattSplit
watches for those steps, matches them against the power you told it each device
draws, and for devices that already have an on/off entity it learns the missing
power from the steps that line up with their state changes. Everything is
local, explainable, and reports a confidence.

## Supported setups

List every sensor that measures energy in your home and give it a role (house
total, grid meter, load, solar / generation, battery). Several of each are
fine. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#measurement-sources-and-roles)
for what each combination can and cannot do, including what a slow sensor (for
example a solar inverter reporting once a minute) can resolve.

Tested on Home Assistant 2026.10; older versions are not verified.

## Development

```bash
python -m pytest
```

The engine (`custom_components/wattsplit/engine/`) has no Home Assistant
dependency; tests run against a synthetic-house simulator in `tests/sim.py`.

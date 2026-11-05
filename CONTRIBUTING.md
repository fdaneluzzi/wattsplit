# Contributing

Thanks for helping. The most valuable contributions are **real-world reports**: setups unlike the author's (single phase, no solar, a different meter, an appliance that is mis-estimated).

## Reporting a problem

Open an issue with:

- Home Assistant version and WattSplit version.
- Your measurement sources: how many, what role, and how often each reports.
- The appliance involved, and whether it has an on/off entity, a power sensor and an energy counter.
- The integration's diagnostics (**Settings > Devices & services > WattSplit > Download diagnostics**). Review the file before attaching it: it lists your entity ids.

## Development

```bash
pip install pytest
python -m pytest
```

- `custom_components/wattsplit/engine/` is plain Python with no Home Assistant dependency. Keep it that way: it is what the tests and the replay tool run.
- The integration layer (`coordinator.py`, `config_flow.py`, `sensor.py`, `repairs.py`, ...) is the only place that imports Home Assistant.
- Add a test with the change. The simulator in `tests/sim.py` builds houses with known appliances, so a test can state what the right answer is.
- Strings: English is the default (`translations/en.json`). Add or change the same keys in every other file under `translations/`.
- Brand images are generated: `python scripts/make_brand.py` (needs Pillow).

## Principles

- Say "unknown" rather than guess. An estimate carries its confidence, and the evidence behind it is visible.
- Never act on a weak inference without asking the user. Hiding a load for good needs overwhelming evidence.
- No UI to build: use what Home Assistant provides (entities, Repairs, the Energy dashboard).

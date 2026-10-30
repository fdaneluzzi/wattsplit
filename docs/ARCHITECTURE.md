# WattSplit — architecture

WattSplit estimates **how much power individual appliances draw, and when they
switch on and off**, using the one signal almost every Home Assistant energy
setup already has: whole-house consumption. It splits the house meter into
per-appliance virtual sub-meters.

## 1. Problem and scope

Three kinds of device show up in a real home, and the system handles all of
them with one mental model:

| Case | What HA knows | What WattSplit produces |
|------|---------------|--------------------------|
| **Measured** | on/off entity **and** a power sensor | Nothing to infer. Used as ground truth: subtracted from the aggregate, and as a self-check and lag calibrator for the engine. |
| **State-only** | an on/off (or `climate`, `switch`, …) entity, no power sensor | Learns the device's power from aggregate steps that coincide with its state changes. Creates power + energy sensors on the *existing* HA device. |
| **Latent** | nothing (electric shower, dishwasher, oven, pump…) | Infers on/off **and** power by matching steps in the aggregate against known power levels, or **discovers** the levels (§4). Creates a new device with `binary_sensor` + power + energy. |

One derived output keeps everything honest: **Untracked** = aggregate −
Σ(everything above). Energy dashboards then always sum to the real total.

Out of scope for v1: fully unsupervised learning with no human in the loop,
cloud services, neural networks.

## 2. Why a custom integration, not an add-on

| | Custom integration | Add-on |
|--|--|--|
| Works on HA OS, Supervised, **Container, Core** | yes | HA OS / Supervised only |
| Creates entities natively, attaches to existing devices | yes | via MQTT/REST workarounds |
| Config UI (config flow, subentries, options) | native | build your own |
| Own web UI | sidebar panel (§7) | ingress |
| Distribution | HACS, later possibly core | add-on repository |
| Compute | light Python + numpy (shipped with HA) | only needed for heavy ML |

The limits of an integration are about *interface*, not capability. What it
gives us: config **subentries** (one per appliance), options flows, actions
(services), recorder access, `Store`, our own SQLite file, executor threads for
heavy work, `panel_custom` + websocket API for a real web UI.

The engine is pure Python with no HA imports, so if heavier compute is ever
needed it can be wrapped in a headless service without a rewrite. That is the
escape hatch; it is not the plan.

## 3. Layers

```
┌───────────────────────────────────────────────────────────────┐
│ Home Assistant layer       custom_components/wattsplit/        │
│  config flow + subentries · coordinator (timer) · entities    │
│  websocket API · sidebar panel · actions · Store · recorder   │
└──────────────┬────────────────────────────────▲───────────────┘
               │ Sample(ts, W)  StateEvent        │ Estimate per device
┌──────────────▼────────────────────────────────┴───────────────┐
│ Engine (pure Python, no HA imports)   .../wattsplit/engine/    │
│  composer → StepDetector → Disaggregator → Estimates           │
│                              ├─ StateDevice  (learns power)    │
│                              ├─ LatentDevice (matches levels)  │
│                              └─ Discoverer   (unexplained →    │
│                                               candidates)      │
│  EdgeLog (SQLite) ──► replay()                                 │
└───────────────────────────────────────────────────────────────┘
```

### Pipeline, per sample tick (default every 2 s)

1. **Compose** the house signal: `grid (+import / −export) + solar + battery
   discharge`. A direct "house consumption" sensor is also accepted. Each input
   has its own configurable **lag** (§6).
2. **Subtract measured devices** to get the *residual*. Detection runs on the
   residual, so a metered A/C no longer pollutes the search for the shower.
3. **Detect steps** (`StepDetector`): median of the last `settle_s` vs. the
   median of the preceding `ref_s`; reported only when both windows are flat
   relative to the step. A ramp yields one edge when it settles.
4. **Attribute each edge** (`Disaggregator.on_edge`):
   1. a **state device** whose entity changed state just before, with the right
      sign. Ambiguous (two toggled together) → dropped, nothing learned;
      implausible against its prior → falls through;
   2. else the **latent device** with the smallest relative error against its
      declared levels, respecting its current level (a 3 200 → 5 500 W setting
      change is a 2 300 W step, not a new shower);
   3. else **unexplained** → handed to the `Discoverer`.
5. **Log** the edge and every state event to SQLite (`EdgeLog`).
6. **Publish** `Estimate(is_on, power_w, confidence, source)` per device.
   Energy (kWh) is integrated in the HA layer so it survives restarts and works
   with the Energy dashboard (`total_increasing`).

## 4. Power modes — when the user tells us the power, and when not

Per device, the user chooses (`DeviceSpec.power_mode`; derived automatically
from what was declared when left unset):

| Mode | Meaning | Applies to |
|------|---------|------------|
| `known` | Declared levels used as-is; nothing learned. | state, latent |
| `prior` | Declared level is a hint; refined from observations (steps more than 50 % off the prior are rejected). | state (latent: treated as `known` for now) |
| `learn` | Nothing declared. State devices learn from toggles; latent devices wait for **discovery**. | state, latent |
| `pinned` | A learned value the user froze. Never touched again. | state |

### Discovery (the inbox)

Edges nobody explains are not discarded. The `Discoverer` pairs each unexplained
rise with a matching fall (same magnitude ±10 %, within 4 h), clusters pairs by
level, and exposes **candidates**: *"≈ 4 400 W, 3 times, ~10 min each, around
19:00"*. The user names a candidate in the panel, which assigns it to a latent
device (`assign_candidate`), and from then on that load is recognised live.
One-off events never become candidates (default minimum: 2 occurrences).

## 5. Learning over time

1. **Online:** robust medians per device, updated on every attributed step.
2. **Discovery:** clustering of unexplained edges (§4).
3. **Replay & refit:** the SQLite log can be re-fed to a fresh engine with
   different parameters (`replay(log, specs, match_window_s=…)`) and the results
   compared. Nightly (executor) refinement is planned: non-negative least
   squares for multi-level devices and drift tracking.
4. **Feedback:** *confirm / reject* on an attribution becomes a constraint
   (M3).
5. **Ground truth:** measured devices validate the engine and give the user an
   accuracy figure for their own house.

Safety rules: pinned/declared values are never overwritten by learning; model
snapshots allow rollback (M3); wrong learning is worse than slow learning, so
ambiguous events teach nothing.

### Why an edge log instead of raw history

The recorder keeps raw states ~10 days by default and long-term statistics are
5-minute resolution, too coarse to see steps. The **edge log** (time, delta,
attribution, plus state events) is tiny, survives recorder purges, and is enough
to re-run attribution. SQLite is used for robustness and queryability; calls go
through the executor. A retention policy (`EdgeLog.prune`) bounds growth.

## 6. Known hard problems

1. **Meter/solar latency mismatch.** Meters update every second, inverters every
   30–300 s; naive composition creates fake steps. Each input gets a **lag**
   setting; the composer tracks staleness; auto-calibration estimates the lag by
   matching edges of the inverter and the meter (or a measured device as
   reference). Replay lets the user try a lag on past data and compare. This is
   the biggest real-world error source and has its own M2 task.
2. **Sparse updates.** HA sensors report on change, so a constant load emits no
   samples. The HA layer ticks on a timer and does sample-and-hold; the engine
   sees a regular grid.
3. **Simultaneous events.** Two steps within `settle_s` merge into one and are
   reported unexplained rather than guessed; M3 adds a combination matcher.
4. **Variable-power devices** (inverter A/C, multi-stage dishwasher). v1 learns
   one level per device; M3 adds NNLS multi-level learning.
5. **Look-alike devices** (two showers) → time-of-day priors and user feedback.
6. **Brazilian wiring (127/220 V, bi/tri-phase).** A 220 V shower steps two
   phases. Per-phase aggregate support and per-device phase assignment are M3;
   v1 treats the aggregate as one channel.
7. **Honesty.** Every estimate carries `confidence` and `source`
   (`declared`, `learned_from_state`, `inferred_from_edges`,
   `awaiting_discovery`). The UI says "estimated", never "measured".

## 7. User interface (no app, no add-on)

Three layers, in order of effort:

1. **Config flow + subentries + options flow:** aggregate sources (with lag),
   one subentry per appliance with its power mode.
2. **Entities + stock cards:** history graphs, stacked charts, diagnostics.
3. **WattSplit sidebar panel** (admin only), served by the integration through
   `panel_custom` and a websocket API. A Lit web component, bundle committed to
   the repo (HACS ships it as-is). Screens:
   - **Timeline:** aggregate power with coloured bands per attributed device
     and an "untracked" remainder; click an edge to see why it was attributed.
   - **Discovery inbox:** candidates with level, count, duration, typical hour;
     name / merge / dismiss.
   - **Devices:** power mode, levels, pin, confidence over time.
   - **Tune & replay:** change lag or window, replay the last N days, compare
     before/after, mark ranges to ignore.

The panel is real frontend work and is scheduled with the engine's learning
features (M2), not deferred.

## 8. Entities created

Per latent device: `binary_sensor.<name>_running`, `sensor.<name>_power` (W),
`sensor.<name>_energy` (kWh).

Per state-only device: `sensor.<entity>_estimated_power`, `…_estimated_energy`,
attached to the **existing** device in the registry.

Global: `sensor.wattsplit_untracked_power`, `…_untracked_energy`,
`…_tracked_share` (%).

Inferred entities expose `confidence`, `source` and `power_mode` attributes.

## 9. Positioning

- **Powercalc** estimates power from fixed profiles (mostly lights). It never
  looks at the aggregate, so it cannot discover a shower. Complementary: a
  Powercalc sensor can be a *measured* input here.
- **ha-nilm** (add-ons, TensorFlow Lite, labelled training on recorder history)
  is a heavier, ML-first design needing a training server and ≥ 4 GB RAM.
  WattSplit is the lightweight, explainable, in-integration alternative that
  works the moment you type the shower's wattage, and uses everything HA
  already knows.

## 10. Roadmap

| Milestone | Content | Status |
|-----------|---------|--------|
| **M0 — engine core** | composer, step detector, latent + state devices, power modes, discovery, SQLite edge log, replay, simulator, tests | **done (17 tests)** |
| **M1 — HA shell** | HACS metadata, config flow + subentries, coordinator with timer sampling, entities, state-event capture, Store persistence | next |
| **M2 — learning + panel** | per-input lag + auto-calibration, recorder bootstrap, sidebar panel (timeline, inbox, devices, tune & replay), diagnostics/self-validation | |
| **M3 — harder devices** | NNLS multi-level, combination matcher, per-phase aggregates, feedback actions, model snapshots | |
| **M4 — community** | device-profile library (JSON, like Powercalc's), docs, HACS default-repo submission, Silver quality scale | |

## 11. Open decisions

- License (MIT vs. Apache-2.0).
- GitHub owner/repo and `codeowners` for the manifest (placeholders today).
- Panel frontend stack details (Lit + Vite is the default assumption).

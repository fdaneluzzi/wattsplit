# WattSplit — architecture

WattSplit estimates **how much power individual appliances draw, and when they
switch on and off**, using the one signal almost every Home Assistant energy
setup already has: whole-home consumption. It splits the whole-home signal into
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
| Own web UI | none needed: entities, Repairs, Energy dashboard (§8) | ingress |
| Distribution | HACS, later possibly core | add-on repository |
| Compute | light Python + numpy (shipped with HA) | only needed for heavy ML |

The limits of an integration are about *interface*, not capability. What it
gives us: config **subentries** (one per appliance), options flows, actions
(services), recorder access, `Store`, our own SQLite file, executor threads for
heavy work, Repairs flows and the Energy manager.

The engine is pure Python with no HA imports, so if heavier compute is ever
needed it can be wrapped in a headless service without a rewrite. That is the
escape hatch; it is not the plan.

## 3. Layers

```
┌───────────────────────────────────────────────────────────────┐
│ Home Assistant layer       custom_components/wattsplit/        │
│  config flow + subentries · coordinator (timer) · entities    │
│  Repairs · Energy dashboard · actions · recorder              │
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
   1. a **state device** that already knows its power (declared, pinned, a
      prior, or an unambiguous learned value) and sees a step of that size
      within the match window (default 90 s) after one of its state changes
      *claims* the edge. Steps that are merely plausible are stored as
      learning candidates but not claimed (see "Consensus learning" below);
   2. else the **latent device** with the smallest relative error against its
      declared levels, respecting its current level (a 3 200 → 5 500 W setting
      change is a 2 300 W step, not a new shower);
   3. else **unexplained** → handed to the `Discoverer`.
5. **Log** the edge and every state event to SQLite (`EdgeLog`).
6. **Publish** `Estimate(is_on, power_w, confidence, source)` per device.
   Energy (kWh) is integrated in the HA layer so it survives restarts and works
   with the Energy dashboard (`total_increasing`).

### Measurement sources and roles

The engine does not care what the hardware is. A home can have several solar
systems, several inverters, several meters, per-phase sensors, a battery. The
user lists every sensor that measures energy and gives each a **role**, which
decides how its readings are read and used (`engine/plan.py`, pure Python):

| Role | Meaning | Used for |
|------|---------|----------|
| `house` | a ready-made whole-home consumption sensor | the whole-home value; one detection channel |
| `grid` | net power at the utility connection (import +, export −) | detection; part of rebuilt consumption |
| `load` | power consumed on a phase, a circuit or the whole house | detection, each one its own channel |
| `generation` | solar inverters, generators (positive when producing) | rebuilt consumption; **evidence only** |
| `battery` | positive when discharging into the house | rebuilt consumption; **evidence only** |

Consumption shown to the user (house power, untracked power, energy) is the
`house` sensor if there is one, else the sum of the `load` sensors, else
`grid + Σ generation + Σ battery`. Detection prefers `load` sensors, then
`house`, then the `grid` meters; generation and battery are added to the
detection signal only when they report about as fast as the slowest grid meter,
otherwise they stay evidence. Several of any role are fine.

| Your setup | Configure | What happens |
|------------|-----------|--------------|
| One whole-home sensor | `house` | one channel: step consensus; channel means only if the signal is quiet; otherwise an honest "don't know yet" |
| Utility meter + one or more inverters (+ battery) | `grid` + `generation` (+ `battery`) | steps on the grid meter; every inverter and the battery are judged per appliance and their shares added |
| Per-phase / per-circuit load sensors | `load` | every sensor its own channel; a quiet one isolates small appliances |
| Two utility meters, a sub-board | several `grid` | each its own channel |
| An appliance fed entirely by an inverter | `generation` | with an on/off entity: its power is learned from the inverter's response, if it runs at least `shortest_run_s` of that channel (~3.6 min at 61 s). With a declared power and no entity: detected from steps on the inverter, delayed by ~1.5 reports |

**Evidence channels.** Generation and battery sensors are never searched for
loads: a cloud steps an inverter exactly as a load would. For a state device
each is judged on its own against the device's on/off changes (median change
statistically clear, consistent across toggles), and the accepted shares are
summed. For a latent device with a **declared** power, steps on an evidence
channel may confirm it, but unexplained steps there never reach the discovery
inbox (`on_edge(discover=False)`, and `replay(restricted_channels=…)`).

**Limits stated up front.**

- Each channel has its own windows, derived from its own reporting rate. A
  channel that reports every 61 s resolves runs of about 3.6 minutes and longer
  (`Timing.shortest_run_s`); shorter runs are measured on faster channels only,
  and the estimate is then a lower bound.
- A latent device whose power is split between channels with different lags
  (part on the 15 s grid, part on the 61 s inverter) cannot be reassembled into
  one step; one that sits wholly on one channel is fine.
- A cloud *recovery* the same size as a declared device can confirm it by
  chance on an evidence channel.
- Sign conventions are assumed: generation positive when producing, battery
  positive when discharging. If a sensor reports the opposite, its contribution
  will not be accepted rather than subtracted.

**Built to be distributed.** No entity names or hardware are assumed anywhere;
all inputs are optional beyond one consumption source; reporting rates are
measured, never configured; every assumption above is an option or shown in the
`House power` sensor's attributes (`mode`, `detecting_on`, `evidence_only`,
`update_period_s`, `min_step_w`, `shortest_run_s`); a diagnostics download
(`diagnostics.py`) contains the plan, rates, timings, per-channel statistics and
each appliance's evidence, for bug reports; strings are in English and
Brazilian Portuguese. `hacs.json` declares the Home Assistant version it was
actually tested on.

### Accuracy you can check

Accuracy is not asserted, it is measured on the user's own house. Any appliance
that has **both an on/off entity and a power sensor** is ground truth. The
engine is run on it a second time as if the sensor did not exist (a shadow
`StateDevice` learns from the whole-home channels around its state changes,
another measures the same thing from the real sensor, with identical windows)
and the `Accuracy check` sensor reports the mean absolute error in percent,
with per-appliance detail (estimate, truth, error, confidence, toggles, which
channels). Only the channel-means evidence is scored; the shadow sees no step
edges. The shadow restarts empty with the integration.

### Energy counters and the Energy dashboard

Home Assistant's Energy dashboard already knows what WattSplit needs: which
sensor is the grid power, which is the solar power, and which appliances have an
energy counter. WattSplit reads it (`energy_import.py`) instead of asking twice.

**Counters as a starting point.** Many appliances (the author's LG air
conditioners and washer) report cumulative energy but no instantaneous power.
Measured on the author's house, a `*_energy_today` counter updates **every 60
minutes** and a `*_energy_this_month` counter roughly **every 5 hours**: far too
slow for real-time attribution, but a clean, interference-free measurement of
how much energy the appliance used between two updates. With the on/off timeline
this gives its power directly (`engine/counter.py`):

`power while on = Σ energy between updates ÷ Σ time on in those intervals`

Intervals with under 5 minutes on are ignored; a reading lower than the last one
is a rollover (daily or monthly counter) and drops that interval; below 10 W the
counter is not measuring a running device. Confidence grows with the number of
usable intervals, the hours of on-time, and shrinks with the scatter between
intervals. A conclusive counter estimate takes precedence over what the
whole-home channels say, and the two are **cross-checked**: the `Accuracy check`
sensor also reports how far each appliance's channel estimate is from its own
counter. Source label: `learned_from_counter`.

**No cold start.** When an appliance with a counter is first set up, the last 10
days of the recorder's state changes and counter readings are copied into the
edge log, so the first replay already has an estimate instead of "unknown".

**Import.** In the options, "Import from the Energy dashboard" fills empty
source lists (grid, solar, battery power sensors) and adds one appliance per
individual device. For each, the on/off entity is guessed among the other entities
of the same device (`engine/states.py`: climate, water heater, humidifier, fan,
then a status sensor that knows running from idle, then a running/power binary
sensor, then a plain power switch; lighting and eco switches are never chosen)
and the finest counter of that device is used (a `*_energy_today` beats the
dashboard's monthly one). Status sensors that report text (a washer's cycle:
`running`, `rinsing`, `spinning`, ...) are read as on/off, and an unknown word is
never read as off.

**First real result (the author's house, 2026-10-30).** Importing the Energy
dashboard created 7 appliances (six LG air conditioners and a washer); each
arrived with 61 to 120 counter intervals copied from the recorder's last 10 days,
and an estimate from the first moment instead of "unknown". Average power while
on, and confidence:

| Appliance | Power while on | Confidence | Usable intervals |
|-----------|---------------:|-----------:|-----------------:|
| Bedroom 1 A/C | 732 W | 0.94 | 73 |
| Veranda 2 A/C | 1253 W | 0.88 | 33 |
| Bedroom 2 A/C | 51 W | 0.74 | 79 |
| Washer | 213 W | 0.65 | 65 |
| Veranda 1 A/C | 1874 W | 0.63 | 4 |
| Living room A/C | 684 W | 0.60 | 34 |
| Dining room A/C | 689 W | 0.48 | 19 |

These are the *average draw while the entity reads "on"*, which is what energy
accounting needs. They are not the compressor's power: a climate entity without
`hvac_action` is "on" in any mode but off, so an A/C that mostly idles in cool
mode (Bedroom 2, 51 W) shows a low average. Veranda 1 rests on only 4 usable
intervals and should be read with that in mind.

Two defects found by this run, both fixed: the recorder returns `LazyState`
objects that have no `.domain` (read it from `entity_id`), and the first
confidence formula penalised the spread *between* intervals, which only reflects
how differently the appliance was used, so appliances with dozens of intervals
came out at 0. The confidence is now the standard error of the total ratio. A
failure while reading history must never stop setup; it is now caught and logged.

**Limits.** The counter is the appliance's own estimate, usually modelled rather
than metered, so it is a good starting point and a cross-check, not exact truth.
At one reading an hour, a washer cycle of an hour and a half is mixed with the
hours around it; the ratio over many cycles is what is trusted.

### What the user sees, without building a dashboard

A community integration cannot depend on every user assembling their own
dashboard, so everything the user needs ships inside the integration and uses what
Home Assistant already provides. No sidebar panel is planned.

- **Settings > Repairs.** Each of the biggest recurring unexplained loads (at most
  3, at least 3 occurrences, at least 150 W, and only groups whose runs last about
  the same) becomes a fixable item: "WattSplit saw a load of about 7932 W switch on
  4 times over 3 days, about 15 minutes each, around 19h, and none of your
  appliances explains it. What is it?" A load that happens at many hours of the day
  says "at 11 different hours of the day" instead of inventing a time. The item
  ends with the latest five runs (`2026-11-10 18:42–18:49 (7 min, 1340 W)`), so the
  user can match them against what they did; the `Discovered loads` sensor adds runs
  by hour of day and by length. The form offers: it is part of the appliance suggested
  (when one is on far more often than usual at that hour of the day), it is one of
  my appliances that has no sensor (assign its power), or it is a new appliance
  (give it a name; WattSplit creates it and re-attributes the past). A dismissed
  item stays dismissed; an assigned one leaves the inbox. (`repairs.py`)
- **The Energy dashboard.** In the options, "Add estimates to the Energy
  dashboard (once)" adds the estimated energy of appliances with no energy counter
  of their own (a pool pump, a shower) as individual devices, so they stop being
  part of "untracked consumption" there. Appliances that already have a counter
  are skipped (they are already in the dashboard, and adding the estimate would
  count them twice); nothing is ever removed. Opt-in, because it edits the user's
  Energy configuration. (`energy_import.py`)
- **Entities.** Internals (discovered loads, accuracy) are `diagnostic` entities,
  every entity has an icon, and each appliance's power sensor exposes `evidence`
  (usable counter intervals, toggles, the cycle levels it owns) so the user can see
  how much the estimate rests on.

### Loads that belong to a running appliance

A compressor cycling on and off while an air conditioner runs produces steps no
state change explains, and they used to fill the discovery inbox (on the author's
house, 107 steps of 1342 W in 6 days). The engine now compares, for each recurring
load, how often a state device was on when it appeared **against how often that
device is on at those same hours of the day** (comparing with its overall on-time
blamed the office A/C for everything that happens in the afternoon). When one is on
far more often than usual:

- it is **suggested** in the Repairs item (lift of at least 2.5 and on in at least
  30% of the occurrences), and confirming it stores the level on the appliance
  (`cycle_levels`), after which steps of that size are its own while it runs;
- it is **adopted without asking** only with overwhelming evidence (20 or more
  occurrences, lift of 6 or more, on in 70% or more of them). A first version
  adopted at 10 occurrences and 50% and, on real data, silently hid a 692 W load
  from the inbox as a 300 W air conditioner's cycle; acting without asking hides a
  load for good, so the bar is high and the rest is a question.

### What counts as "the same load" in the inbox

Grouping by power alone is wrong: a pressure pump (1.34 kW, 6-minute cycles), a
washer's heater and a compressor can all draw about the same, and a rise whose fall
was lost gets paired with someone else's fall and becomes a "run" of hours. The first
version of the Repairs text therefore said "107 times around 17h" for what was
really several sources over 6 days. Now the pairs of a power cluster are split by run
length: the group around the median length (from a quarter to four times it) is one
candidate, shorter and longer runs are grouped separately. Each candidate reports
its distinct days, the distinct hours of day, and the 10th/90th percentile of its run
length, so the text states only what is true (`N times over D days`, `around Hh` only
when it happens in 4 hours or fewer). A candidate whose runs are too spread to be
one source is not offered as a question. A device can also not be suggested as owner
of a load more than 3 times its own learned power (a 300 W appliance does not cycle
1.3 kW).

### Smaller fixes behind it

- Toggle evidence per channel was capped at 10 events (the cap of 40 records was
  set when one record held every channel); it is now 600 records.
- The discovery inbox remembered 500 edges (about 2 days); it now keeps 5000.
- State changes are confirmed only after holding 20 s (`StateDebouncer`): an entity
  that reads off, on, off within seconds (the washer 17 times in 94 changes) no
  longer pollutes the timeline. The confirmed change keeps its original timestamp.
- The estimated-energy sensors integrate every 2 s but publish at most once a
  minute (or when the appliance stops, or availability changes): a state per tick
  was about 43 000 recorder rows a day per running appliance.
- The accuracy sensor also reports a weak cross-check (channels below the strict
  significance bar against the energy counter), marked `weak`, so it is not
  `unknown` while the evidence builds up.
- Recomputing the inbox at most once a minute and one pass per device for the
  association took `candidates()` from 38 ms to 2.7 ms on 6 days of data.

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
19:00"*. Each good candidate becomes a Repairs item; answering it assigns the load to
a latent device (`assign_candidate`, also available as an action), and from then on
that load is recognised live.
One-off events never become candidates (default minimum: 2 occurrences).

## 5. Learning over time

1. **Consensus learning (state devices):** a real house produces about one
   unrelated step per minute and appliances lag their state change by 7–60 s
   (found on the first real test, see below), so "the first plausible step" is
   wrong. Each state change collects *all* sign-correct steps in the window;
   the device's power is the magnitude that recurs across the most events.
   Unrelated loads rarely repeat the same size after every toggle. Confidence
   falls when a rival magnitude has similar support (an ambiguous consensus
   never claims steps), so a wrong guess does not sound sure. On 60 simulated
   busy houses it is right in 58 and the two misses report confidence < 0.1.
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

## 6. Sessions and user corrections

The two device families get two different promises:

1. **Monitored by HA** (has an on/off entity, e.g. the office A/C). The user
   does **not** declare a power. The state entity is the authority on *when*;
   power is learned by consensus (§5). A declared power is only an optional
   hint.
2. **Not monitored** (shower, dishwasher). The user **declares the power**
   (one value, or several for multi-stage devices); WattSplit infers *when*. It
   keeps a history of runs, and the user can correct it. Discovery (§4) remains
   as an assist for finding the number, not the default path.

The hard case is look-alikes: a shower and a dishwasher that draw nearly the
same watts cannot be told apart by power. They *can* be told apart by how long
they run and when, so attribution moves from single steps to **sessions**.

### Sessions

A **session** is one continuous run of a device: start, end, level(s), energy.
For a latent device it is a rise paired with its matching fall (plus level
changes in between); for a state device it comes from the state changes. They
live in the SQLite log (`sessions`: device, start, end, level, energy, status
`open`/`closed`, `source` `auto`/`user`, per-candidate probabilities, label).

### Scoring

For each candidate device `d`, the session's score is

`log P(level | d) + log P(duration | d) + log P(hour of week | d) + log prior(d)`

normalised across all candidates plus "none of them".

- `P(level | d)`: tolerance around the declared levels.
- `P(duration | d)`: log-normal learned from the user's *confirmed* sessions,
  with an optional declared "typical run time" as a weak starting prior.
- `P(hour | d)`: smoothed 24-bin histogram, also from confirmed sessions.

At the **rise** the duration is unknown, so the open session is scored on level
and hour only. The device is shown as running when its probability passes a
threshold, and the probability is exposed as an attribute. At the **fall** the
session is rescored with its real duration and may be reassigned; history is
corrected, while the live state during the first minutes may have been wrong.
That trade-off is inherent and is shown to the user, not hidden.

### Corrections

The user can confirm a session, reassign it to another device, mark it "not one
of mine", delete it, split or merge sessions, and adjust start/end. Each
correction is stored as a **label**:

- labelled sessions are ground truth: replay never overrides them;
- only **confirmed** sessions update a device's duration and hour priors, so a
  wrong guess nobody reviewed cannot reinforce itself (no confirmation bias);
- once a device has a few confirmed sessions its priors become data-driven;
  before that, level dominates and the UI says confidence is low.

Because replay is deterministic and labels live in the log, changing a label or
a parameter reprocesses history reproducibly.

### Limits we state up front

- Corrections change WattSplit's own history and future inference. Home
  Assistant's long-term statistics for energy already published are **not**
  rewritten automatically (an optional statistics-import step is an open
  decision).
- At setup, if a new latent device's power is within ±15 % of another's, the
  user is warned that separation will depend on run time, hour and corrections.

## 7. Known hard problems

1. **Sensors report at very different rates.** A local meter may report every
   second, a grid meter every 15 s, a cloud inverter every 60 s or slower, and
   every household differs. Nothing is hard-coded: the HA layer measures each
   input's real reporting period from `last_reported` (`RateTracker`) and derives
   every window from it (`timing_for`): a step must outlast at least one more
   report, per-channel steps merge within ~1.2 reports, and the averaging
   windows around a state change grow to ~8 reports (never under 120 s). Timing
   is recomputed as the measurements firm up, and the detector rebuilt when it
   moves by more than 30 %. If a sensor is too slow to resolve a phenomenon (a
   3-minute A/C cycle behind a 61 s sensor), the estimators do not reach
   confidence and the device reports that it does not know; they never invent a
   number.
2. **Which signal to look for steps in.** Steps are best seen in the fastest,
   least delayed signal that contains them. A slow generation sensor is kept out
   of the detection signal: its delayed copy of the solar output adds steps of
   its own (`grid + lagged solar` wanders by hundreds of watts). Detection runs
   on the grid / house / per-channel signals; `grid + solar + battery` is only
   composed for display, untracked power and energy, and detection uses it only
   when solar reports about as fast as the grid. The `house` sensor passes through
   a glitch filter: an exact 0 W after a loaded signal is held back for up to
   90 s.

   **But an appliance may be fed by the grid, the inverter, or both**, and the
   engine must not assume where its power shows up. Generation (and battery)
   sensors are therefore **evidence channels**: they are *never* searched for
   steps (a cloud passing over the array is not a load), but for a state device
   each one is judged on its own against the device's on/off changes, exactly
   like a phase. Each channel's median change is accepted only if it is
   statistically clear (z ≥ 6) and consistent across toggles, and the
   accepted channels' shares are **summed**: 600 W on the grid plus 400 W on the
   inverter is a 1000 W appliance. Which sensors count as evidence is an option
   (default: the entry's solar/battery sensors, when detection runs on the
   net-grid signal, which excludes generation; a house or composed signal
   already contains them).

   Every channel uses its own averaging windows, derived from its own reporting
   rate, so a 61 s inverter does not slow a 15 s phase down. The cost is stated:
   an appliance that cycles faster than a slow channel can resolve (a 3-minute
   A/C versus ~8-minute windows on a 61 s inverter) is measured on the fast
   channels only, and its estimate is a lower bound. Appliances that stay on
   for 10+ minutes also get the slow channel's share.

   What a given sensor *means* (net grid, load, per-phase net, inverter output)
   is not guessed: the engine only reads which channels move with a state
   change. In the author's data over 3.7 h, 60 s changes in the phase signals
   track the grid sensor (r = 0.53) and not the inverter (r = 0.00), but that
   says nothing about other installations.
3. **Sparse updates.** HA sensors report on change, so a constant load emits no
   samples. The HA layer ticks on a timer and does sample-and-hold; the engine
   sees a regular grid. The tick (default 2 s) is independent of the sensors'
   rates; it only needs to be faster than they are.
4. **Simultaneous events.** A house always has several things switching. Two
   steps inside the same meter report (15 s) arrive as one merged edge, so a
   single step cannot tell them apart. What helps: (a) every merged edge keeps
   its per-channel `parts`; (b) a state device with a clear *footprint* (the
   channels it lives on, learned from its toggles) takes its own share of a
   merged edge, and what is left goes on through the normal attribution. Real
   case from the author's house: a 220 V pump (phases A + C, 2.3 kW) switched
   off in the same report as the office A/C (phase B, 0.3 kW); the merged
   -2616 W step matched nothing, but with the A/C's share taken out the
   remaining -2279 W is recognisable as the pump. Unrelated interference that is
   not simultaneous with a toggle averages out as events accumulate (robust
   medians, error shrinking with the square root of the number of toggles); it
   needs no clean experiment. Still open: an ordinary joint fit of all known
   state indicators at once (M3), so that known loads are subtracted rather than
   treated as noise.
5. **Variable-power devices** (inverter A/C, multi-stage dishwasher). v1 learns
   one level per device; M3 adds NNLS multi-level learning.
6. **Look-alike devices** (shower vs. dishwasher) → session-level scoring on
   level, duration and hour of week, trained by user corrections (§6).
7. **Wiring and measurement layouts (single-phase, split-phase, three-phase,
   sub-panels).** The engine knows *channels*, not phases: one channel for a
   single-sensor home, N for anything else. A load that spans channels (a 220 V
   shower on two phases) is merged into one edge with its per-channel `parts`;
   a state device's contribution is learned per channel and only channels where
   it is statistically clear are counted. Noisy channels never veto quiet ones,
   and a home with one noisy sensor still gets step-based learning or an honest
   "don't know".
8. **Honesty.** Every estimate carries `confidence` and `source`
   (`declared`, `learned_from_state`, `inferred_from_edges`,
   `awaiting_discovery`). The UI says "estimated", never "measured".

## 8. User interface (no app, no add-on, no panel)

A community integration cannot depend on every user building a dashboard, and a
sidebar panel is a large frontend to maintain, so the interface is what Home
Assistant already provides:

1. **Config flow + subentries + options flow:** measurement sources (with their
   roles), one subentry per appliance, and a few tuning options.
2. **Entities:** estimated power and energy per appliance (attached to the
   appliance's own device when it has one), house and untracked power, and
   diagnostic entities (discovered loads, accuracy check).
3. **Repairs:** the discovery inbox as fixable questions (see "What the user sees,
   without building a dashboard" above).
4. **The Energy dashboard:** import appliances from it and, opt-in, add estimates
   to it.

## 9. Entities created

Per appliance: `sensor.<name>_estimated_power` (W) and `sensor.<name>_estimated_energy`
(kWh), attached to the appliance's existing device when it has one; a `Running`
binary sensor for appliances WattSplit infers.

Global: `House power`, `Base load` (the 5th percentile of the per-minute whole-home
power over the last 24 h, available after an hour of data), `Untracked power`,
`Untracked energy`, and the diagnostic `Discovered loads` and `Estimate error` (the
mean absolute difference in percent between the estimates and the real sensors or
counters that can be compared; its entity id keeps the old `accuracy_check` key).

Inferred entities expose `confidence`, `source`, `power_mode` and `evidence`
attributes. Entity names come from the translations; English is the default.

## 10. Positioning

- **Powercalc** estimates power from fixed profiles (mostly lights). It never
  looks at the aggregate, so it cannot discover a shower. Complementary: a
  Powercalc sensor can be a *measured* input here.
- **ha-nilm** (add-ons, TensorFlow Lite, labelled training on recorder history)
  is a heavier, ML-first design needing a training server and ≥ 4 GB RAM.
  WattSplit is the lightweight, explainable, in-integration alternative that
  works the moment you type the shower's wattage, and uses everything HA
  already knows.

## 11. Roadmap

Done: engine core (steps, channels, power modes, discovery, edge log, replay,
simulator), Home Assistant shell (config flow, subentries, entities), energy
counters and history bootstrap, Energy dashboard import/export, Repairs inbox,
cycle levels and association, English plus pt-BR.

Next, in rough order:

- Combination matching (a shower step that includes a pump switching on with it).
- Tests for the Home Assistant layer (the engine is well covered, the shell is not).
- Verifying other topologies: single phase, no solar, other meters.
- A device-profile library (typical power of common appliances), like Powercalc's.
- Home Assistant 2026.11 and later.

## 12. Home Assistant API notes (verified against 2026.10.4)

- Schemas: HA moved to `probatio` in 2026.9; `import voluptuous as vol` still
  works through an alias, so the integration keeps that import.
- `async_device_info_to_link_from_entity` is deprecated and returns `None`. To
  put estimates on an existing device, set `entity.device_entry` (via
  `async_entity_id_to_device`). Confirmed live: the A/C sensors share the
  `climate` entity's device.
- Appliances are config **subentries** (`ConfigSubentryFlow`); entities are
  added with `config_subentry_id`; an update listener reloads the entry, which
  replays the log with the new specs.
- Scripted subentry flows over REST: send JSON as UTF-8 (a Windows shell mangles
  accented names; use Python or a file).

## 13. First real-world test (office A/C, 2026-10-30)

Six toggles on a real inverter A/C in a busy house: 26 steps in 26 minutes,
the A/C's own step arrived 7–64 s after the state change, and the composed
house sensor read 0 W for 30 s or more twice (producing fake ±1 kW steps). The
original 15 s first-plausible-step learner got 1 of 6 events; replaying the
same log with consensus learning gives 345 W from 3 agreeing events, at low
confidence (0.15), which is the honest answer for six toggles. Open issue: the
zero readings should be rejected or bridged by the composer (M2).

### Second round: what the signal actually looks like

Six more toggles, this time compared against the raw sensors:

- **Update rates:** grid meter every ~15 s (max 60 s), solar inverter every
  ~61 s. Sampling at 2 s oversamples; the real resolution is ~15 s.
- **The composed house signal is the worst available.** Net export is ~2–3 kW
  and follows clouds instantly on the grid meter while the inverter reports a
  minute late, so `grid + solar` wanders by hundreds of watts, the same order
  as a small appliance. The detector found 3 steps in 21 minutes and only 2
  of 6 toggles produced a candidate.
- **Per-phase data isolates the appliance.** The office A/C sits on phase B,
  which idles at ~145–160 W while phases A and C carry 1–1.7 kW of other loads
  and solar. On phase B every toggle is visible (+142, +165, −166, −130, −141,
  −315 W steps).
- **The A/C is not a constant load.** It starts near 150 W and ramps to
  300–570 W as the inverter compressor speeds up, so one number is a rough
  average.
- **On/off mean difference beats step matching.** Comparing the mean power of
  each ON window with its neighbouring OFF windows (skipping the first 40 s)
  over the 6 cycles gives, per signal: phase B **176 W, spread 43 W**; grid
  total 276 W, spread 366 W (one cycle even the wrong sign); composed house
  443 W, spread 169 W. The earlier step-consensus value (333 W) was inflated
  by ramp peaks.

Consequences, in priority order (M2a): (1) **multiple input channels**
(per phase, each with its own noise-adaptive threshold) instead of a single
aggregate; (2) a **difference-of-means estimator** for state devices, with
edges kept for latent devices; (3) reject/bridge impossible readings and
account for per-input update rates; (4) model non-constant power (mean plus a
time-since-start profile); then sessions and corrections (§6).

### Third round: live, three phases as channels

Eight toggles with the new engine running on the three phase sensors (15 s).
Result: **7 valid toggles, no channel accepted, confidence 0.06** (the old
step-consensus value of 345 W stays as a weak fallback). Reasons, from the log:

- One toggle pair was lost for a good reason: the climate entity flipped
  off, on, off within 11 s, and the windows around such flaps are too short to
  trust.
- The appliance does not have one power. Its contribution on phase B over seven
  toggles was +65, +322, +197, +205, +422, +387, +185 W (median 205, scatter
  174), and phase C moved with it (median +242 W, consistent on 6 of 7). A 220 V
  inverter unit sharing phases B and C at a varying thermal load fits this.
  With scatter this large, seven toggles give z of about 2.5 to 3.7, below the
  bar of 6, so the engine declines to commit. Reaching it needs more toggles
  (roughly 30), or a model of why the power varies (M3).
- The display-only provisional value is now the sum of channels that lean the
  same way on most toggles: 447 W (B + C), consistent with the 276 to 443 W
  seen from the composite signals in the earlier rounds. A noise channel no
  longer inflates it (it was 517 W when every channel was summed).

Take-away: for an appliance whose power is genuinely variable, the engine is
slow and says so, rather than committing to a number the data does not support.

## 14. Open decisions

- Whether to submit to the HACS default repository once there is feedback from
  setups other than the author's.

# Changelog

## 0.5.2 - 2026-11-12

- Run limit of an appliance with no entity: until it has finished five runs it is capped at 2 hours (it used to have no limit), and afterwards the limit follows the median and third quartile of its own runs (at least 15 minutes, eight times the median) instead of the 90th percentile, so a stuck run among the first few cannot raise it for good. Three similar runs that end by a down-step past the limit teach it a real long cycle. Suggested by Squixx on issue #3.

## 0.5.1 - 2026-11-12

- Fix: an appliance with no entity could stay "on" for hours after a missed down-step (issue #3), inflating its energy and zeroing the untracked power. Now a rise of the appliance's own size while it is "on" starts a new run, an inferred run is closed when the whole-home power is far below what the running inferred appliances claim, and a run far longer than any it has finished is closed. A closing step also goes to an open run over a weaker claim by an appliance with an entity.
- The Repairs question for a discovered load now lists its latest runs (date, start-end, length, power), and the `Discovered loads` sensor exposes, per load, runs by hour of day, runs by length and the latest runs (issue #2).
- New `Base load` sensor: what the home draws when nothing is going on (low percentile of the last 24 h of per-minute whole-home power). It is the part of "untracked" no appliance will explain.
- The diagnostic `Accuracy check` is renamed `Estimate error`: its value is a mean difference in percent (lower is better) and it now says how many appliances it compared.
- Fix: the discovery inbox paired a rise with the oldest matching fall, inventing runs of hours when a fall was lost; it now pairs each fall with the most recent rise.

## 0.5.0 - first public beta

- Estimates per-appliance power, energy and on/off from a whole-home signal: grid, per-phase, load, solar and battery sensors, any number of each, any update rate.
- Appliances with an on/off entity learn their power from the house signal and from their own energy counter; appliances with no entity are detected from recurring steps.
- Settings > Repairs asks "what is this?" for recurring unexplained loads (part of an appliance, one of yours without a sensor, or a new appliance).
- Import appliances from the Energy dashboard; optionally add the estimates back to it.
- Learns from the recorder's last 10 days at first start; learning survives restarts through a local edge log.
- English interface with a Brazilian Portuguese translation.

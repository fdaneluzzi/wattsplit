# Changelog

## Unreleased

- Fix: an appliance with no entity could stay "on" for hours after a missed down-step (issue #3), inflating its energy and zeroing the untracked power. Now a rise of the appliance's own size while it is "on" starts a new run, an inferred run is closed when the whole-home power is far below what the running inferred appliances claim, and a run far longer than any it has finished is closed. A closing step also goes to an open run over a weaker claim by an appliance with an entity.
- Fix: the discovery inbox paired a rise with the oldest matching fall, inventing runs of hours when a fall was lost; it now pairs each fall with the most recent rise.

## 0.5.0 - first public beta

- Estimates per-appliance power, energy and on/off from a whole-home signal: grid, per-phase, load, solar and battery sensors, any number of each, any update rate.
- Appliances with an on/off entity learn their power from the house signal and from their own energy counter; appliances with no entity are detected from recurring steps.
- Settings > Repairs asks "what is this?" for recurring unexplained loads (part of an appliance, one of yours without a sensor, or a new appliance).
- Import appliances from the Energy dashboard; optionally add the estimates back to it.
- Learns from the recorder's last 10 days at first start; learning survives restarts through a local edge log.
- English interface with a Brazilian Portuguese translation.

# Changelog

## 0.5.0 - first public beta

- Estimates per-appliance power, energy and on/off from a whole-house signal: grid, per-phase, load, solar and battery sensors, any number of each, any update rate.
- Appliances with an on/off entity learn their power from the house signal and from their own energy counter; appliances with no entity are detected from recurring steps.
- Settings > Repairs asks "what is this?" for recurring unexplained loads (part of an appliance, one of yours without a sensor, or a new appliance).
- Import appliances from the Energy dashboard; optionally add the estimates back to it.
- Learns from the recorder's last 10 days at first start; learning survives restarts through a local edge log.
- English interface with a Brazilian Portuguese translation.

"""Constants for WattSplit."""

DOMAIN = "wattsplit"

SUBENTRY_APPLIANCE = "appliance"

# Where the measurements come from. CONF_GRID, CONF_BATTERY, CONF_LOADS and
# CONF_GENERATION hold lists of sensors; entries made by earlier versions kept
# single sensors in CONF_GRID / CONF_SOLAR / CONF_BATTERY and `channels`.
CONF_GRID = "grid_entity"
CONF_SOLAR = "solar_entity"
CONF_BATTERY = "battery_entity"
CONF_HOUSE = "house_entity"  # a ready-made whole-house consumption sensor
CONF_INVERT_GRID = "invert_grid"
CONF_LOADS = "load_entities"  # per-phase / per-circuit / whole-house load sensors
CONF_GENERATION = "generation_entities"  # solar inverters, generators

# Main entry options.
CONF_SAMPLE_INTERVAL = "sample_interval"
CONF_MIN_STEP = "min_step_w"
CONF_CHANNELS = "channels"  # optional extra measurement channels (e.g. phases)
CONF_EVIDENCE = "evidence"  # extra sensors judged per appliance, never searched for loads

# Appliance subentry.
CONF_STATE_ENTITY = "state_entity"
CONF_POWER_ENTITY = "power_entity"
CONF_POWER_MODE = "power_mode"
CONF_POWER_LEVELS = "power_levels"

DEFAULT_SAMPLE_INTERVAL_S = 2
DEFAULT_MIN_STEP_W = 100  # floor; each channel raises it to clear its own noise
NOISE_K = 4.0

LOG_RETENTION_DAYS = 90
MAX_ENERGY_STEP_S = 60  # never integrate more than this per tick (clock jumps)
STALE_AFTER_S = 900  # an input that has not reported for this long is unavailable
RETIME_CHANGE = 0.3  # rebuild detectors when the measured timing moves this much

"""Constants for WattSplit."""

DOMAIN = "wattsplit"

SUBENTRY_APPLIANCE = "appliance"

# Where the measurements come from. CONF_GRID, CONF_BATTERY, CONF_LOADS and
# CONF_GENERATION hold lists of sensors; entries made by earlier versions kept
# single sensors in CONF_GRID / CONF_SOLAR / CONF_BATTERY and `channels`.
CONF_GRID = "grid_entity"
CONF_SOLAR = "solar_entity"
CONF_BATTERY = "battery_entity"
CONF_HOUSE = "house_entity"  # a ready-made whole-home consumption sensor
CONF_INVERT_GRID = "invert_grid"
CONF_LOADS = "load_entities"  # per-phase / per-circuit / whole-home load sensors
CONF_GENERATION = "generation_entities"  # solar inverters, generators

# Main entry options.
CONF_SAMPLE_INTERVAL = "sample_interval"
CONF_MIN_STEP = "min_step_w"
CONF_IMPORT_ENERGY = "import_energy"  # one-shot: import from the Energy dashboard
CONF_CHANNELS = "channels"  # optional extra measurement channels (e.g. phases)
CONF_EVIDENCE = "evidence"  # extra sensors judged per appliance, never searched for loads

# Appliance subentry.
CONF_STATE_ENTITY = "state_entity"
CONF_ENERGY_ENTITY = "energy_entity"  # the appliance's own cumulative energy counter
CONF_POWER_ENTITY = "power_entity"
CONF_POWER_MODE = "power_mode"
CONF_POWER_LEVELS = "power_levels"

DEFAULT_SAMPLE_INTERVAL_S = 2
DEFAULT_MIN_STEP_W = 100  # floor; each channel raises it to clear its own noise
NOISE_K = 4.0

LOG_RETENTION_DAYS = 90
HISTORY_DAYS = 10  # how far back to learn from the recorder when starting
MAX_ENERGY_STEP_S = 60  # never integrate more than this per tick (clock jumps)
STALE_AFTER_S = 900  # an input that has not reported for this long is unavailable
RETIME_CHANGE = 0.3  # rebuild detectors when the measured timing moves this much

# Confirm a state change only if it holds this long (entities that flap off-on-off).
DEBOUNCE_S = 20.0
# The energy sensors integrate every tick but publish at most this often, or the
# recorder fills with ~43 000 states a day per running appliance.
ENERGY_PUBLISH_S = 60.0
# Discovered loads become Repairs items; show only the biggest few so it never spams.
MAX_ISSUES = 3
ISSUE_MIN_COUNT = 3
ISSUE_MIN_LEVEL_W = 150.0
CONF_CYCLE_LEVELS = "cycle_levels"  # power steps a device makes by itself while it runs
CONF_EXPORT_ENERGY = "export_energy"  # one-shot: add estimates to the Energy dashboard
CANDIDATES_EVERY_S = 60.0  # recompute the discovery inbox at most this often

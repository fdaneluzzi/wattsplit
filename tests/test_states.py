from engine.states import on_off, pick_counter, pick_state_entity

WASHER_OPTIONS = [
    "initial", "refreshing", "spinning", "rinsing", "sleep", "detecting", "reserved",
    "power_off", "running", "pause", "error", "drying", "rinse_hold", "cool_down", "end",
]


def test_plain_entities_follow_their_state():
    assert on_off("switch", "on") is True and on_off("switch", "off") is False
    assert on_off("fan", "ON") is True
    assert on_off("switch", "unavailable") is None and on_off("light", "unknown") is None


def test_a_climate_without_hvac_action_is_on_in_any_mode_but_off():
    assert on_off("climate", "cool", {}) is True
    assert on_off("climate", "off", {}) is False


def test_a_climate_with_hvac_action_is_on_only_while_working():
    assert on_off("climate", "cool", {"hvac_action": "cooling"}) is True
    assert on_off("climate", "cool", {"hvac_action": "idle"}) is False
    assert on_off("climate", "heat", {"hvac_action": "off"}) is False


def test_a_washers_status_text_is_read_as_a_cycle_not_as_a_switch():
    for running in ("running", "rinsing", "spinning", "drying", "rinse_hold", "cool_down"):
        assert on_off("sensor", running) is True, running
    for idle in ("power_off", "initial", "sleep", "end", "pause", "error"):
        assert on_off("sensor", idle) is False, idle
    assert on_off("sensor", "some_new_status") is None  # never guessed as off


def test_every_lg_air_conditioner_of_the_real_house_picks_its_climate_entity():
    device = [
        ("switch.ar_condicionado4_power", "on", {}),
        ("switch.ar_condicionado4_iluminacao", "on", {}),
        ("climate.ar_condicionado4", "cool", {}),
    ]
    assert pick_state_entity(device) == "climate.ar_condicionado4"
    bedroom = [
        ("switch.ar_condicionado_bedroom_economia_de_energia", "off", {}),
        ("switch.ar_condicionado_bedroom_power", "on", {}),
        ("climate.ar_condicionado_bedroom", "off", {}),
    ]
    assert pick_state_entity(bedroom) == "climate.ar_condicionado_bedroom"


def test_the_real_washer_picks_its_status_sensor_over_the_power_switch():
    washer = [
        ("binary_sensor.lavadora_inicio_remoto", "off", {}),
        ("switch.lavadora_power", "off", {}),
        ("sensor.lavadora_status_atual", "power_off", {"device_class": "enum", "options": WASHER_OPTIONS}),
    ]
    assert pick_state_entity(washer) == "sensor.lavadora_status_atual"


def test_a_device_with_only_a_bare_switch_falls_back_to_it_and_nothing_else_returns_none():
    assert pick_state_entity([("switch.pump_power", "on", {})]) == "switch.pump_power"
    assert pick_state_entity([("switch.x_iluminacao", "on", {})]) is None
    assert pick_state_entity([("sensor.x_voltage", "230", {})]) is None


def test_the_finest_counter_wins_over_the_dashboards_monthly_one():
    siblings = [
        "sensor.bedroom_ac_energy_today",
        "sensor.ar_condicionado4_energy_this_month",
        "sensor.ar_condicionado4_energy_yesterday",
    ]
    assert pick_counter(siblings, "sensor.ar_condicionado4_energy_this_month") == (
        "sensor.bedroom_ac_energy_today"
    )
    assert pick_counter(["sensor.x_energy_this_month"], "sensor.x_energy_this_month") == (
        "sensor.x_energy_this_month"
    )

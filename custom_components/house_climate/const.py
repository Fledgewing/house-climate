"""Configuration defaults; entity IDs belong only in configuration."""

DOMAIN = "house_climate"
PLATFORMS = ["climate", "button", "sensor", "switch"]
DEFAULTS = {
    "sensors": [],
    "systems": [],
    "outdoor_sensor": "",
    "weather_entity": "",
    "solar_sensor": "",
    "occupancy_sensor": "",
    "interval": 5,
    "fan_hold_minutes": 30,
    "fan_reward_lambda": 1.0,
    "learn_exclude_modes": "auto",
    "quiet_hours_enabled": False,
    "quiet_start": "22:00",
    "quiet_end": "07:00",
    "stale_after": 720,
    "frozen_after": 4,
    "filter_minutes": 15,
    "max_rate": 0.5,
    "minor_weight_share": 0.05,
    "deadband": 1.0,
    "minimum_dwell": 60,
    "offset_clamp": 3.0,
    "max_offset_change": 0.5,
    "cool_outdoor_min": 10.0,
    "heat_outdoor_max": 24.0,
    "kp": 1.0,
    "ki": 0.1,
    "learning_forgetting": 0.998,
    "learning_n0": 24,
}

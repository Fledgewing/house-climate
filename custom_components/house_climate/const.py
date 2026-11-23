"""Configuration defaults; entity IDs belong only in configuration."""

DOMAIN = "house_climate"
DEFAULT_RUN_THRESHOLD = 50
# Duct supply − return ΔT (°C) → signed learning effort (v0.5.22).
DELTA_DEFAULTS = {"delta_deadband": 2, "delta_full_heat": 15, "delta_full_cool": 10}
# Slow cloud reporters and command-derived learning effort (v0.5.23), per system.
# `report_timestamp_sensor` is an entity whose state is the device's last real
# data fetch time; empty uses the climate entity's own last report time.
REPORT_DEFAULTS = {
    "report_trust": "auto",
    "report_timestamp_sensor": "",
    "report_fresh_minutes": 5,
    "command_effort": True,
}
REPORT_TRUST = ["auto", "fresh", "slow"]
# Command-derived effort constants (v0.5.23), not in the UI: gaps in °C, times
# in minutes. Read with `config.get(key, COMMAND_DEFAULTS[key])`. The start
# delay, restart guard and stop tail are assumed thermostat/compressor timings.
COMMAND_DEFAULTS = {
    "command_clear_gap": 1.5,
    "command_off_gap": 1.0,
    "command_start_delay": 2,
    "command_restart_guard": 5,
    "command_stop_tail": 3,
    # A sent command counts as confirmed during this grace (seconds) even
    # before the slow device echoes it; the same span as outside-change notes.
    "command_confirm_seconds": 120,
    # Contradiction counter: last N cross-checks, suspend/clear rates, minimum
    # checks before a suspension.
    "command_checks": 20,
    "command_suspend_rate": 0.3,
    "command_clear_rate": 0.1,
    "command_min_checks": 10,
}
# Auto report cadence: gaps kept, gaps needed before trusting the median, and
# the median gap (in control intervals) above which a system is slow.
CADENCE_GAPS = 48
CADENCE_MIN_GAPS = 6
CADENCE_SLOW_INTERVALS = 3
PLATFORMS = ["climate", "button", "sensor", "switch", "select"]
DEFAULTS = {
    "sensors": [],
    "systems": [],
    "extra_heat_sources": [],
    "disturbance_sigma": 3,
    "disturbance_intervals": 2,
    "disturbance_clear_intervals": 3,
    "learning_pause_hours": 0,
    "regime_margin": 0.3,
    "regime_hours": 24,
    "regime_return_hours": 4,
    "temporary_hours": 0,
    "outdoor_sensor": "",
    "weather_entity": "",
    "solar_sensor": "",
    "occupancy_sensor": "",
    "interval": 5,
    "fan_hold_minutes": 30,
    "objective": "band",
    "average_weight": 1.0,
    "fan_reward_lambda": None,
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
    "min_run_minutes": 10,
    "min_off_minutes": 5,
    "max_starts_per_hour": 3,
    "switch_penalty": 0.05,
    "effort_change_penalty": 0.005,
    "comfort_band": 1.0,
    "run_margin": 1.0,
    "on_margin": 3.0,
    "off_margin": 3.0,
    "thermostat_hysteresis": 0.5,
    "control_horizon_minutes": 45,
    "effort_hysteresis": 0.05,
    "effort_dwell_minutes": 5,
    "kp": 1.0,
    "ki": 0.1,
    "mean_integral_gain": 0.001,
    "learning_window_minutes": 60,
    "learning_average_minutes": 10,
    "learning_forgetting": 0.998,
    "learning_n0": 24,
    "learning_drift": 0.0,
    "interaction_prior": 0.0001,
    "lag_minutes": 60,
    "lag_prior": 0.01,
    "explore_threshold": 0.8,
    "excite_after_minutes": 120,
    "excite_minutes": 60,
    "test_overshoot": 1.5,
}


def fan_reward_weight(config):
    """An unset fan override follows the allocator, including after options edits."""
    override = config.get("fan_reward_lambda")
    return config.get("average_weight", 1.0) if override is None else override

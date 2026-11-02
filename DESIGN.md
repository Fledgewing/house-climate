# House Climate: design

A Home Assistant custom integration (`custom_components/house_climate`, domain `house_climate`)
that runs several HVAC systems as one house thermostat. It keeps them on the same heat/cool mode
and gives each system its own setpoint so the temperatures across the house stay close together.

## The house today (2026-11-01)

| Role | Entity | Notes |
|---|---|---|
| Whole-house ducted system | `climate.home_zone_1` | Bryant Evolution Connex via `ha_carrier`. Modes off/fan_only/heat_cool/heat/cool. Step 0.5 °C, range 7–35. Presets include `manual` (hold); must stay on `manual`. Its built-in sensor sits in the living room, across the room from the Mitsubishi. |
| Living-room mini-split | `climate.livingroom` | Mitsubishi via `kumo`. Modes off/cool/dry/heat/fan_only/heat_cool. Step 0.5 °C, range 7–35. |
| Room sensors | `sensor.home_zone_1_temperature` (Bryant thermostat, living room), `sensor.livingroom_current_temperature` (Mitsubishi, living room), `sensor.upstairs_temperature_sensor_air_temperature`, `sensor.downstairs_temperature_sensor_air_temperature`, `sensor.office_sensor_air_temperature_2`, `sensor.guest_bedroom_sensor_air_temperature` | Spread on 2026-11-01 was about 6 °C (16.9 to 23.0). |
| Outdoor | `sensor.home_outdoor_temperature` (Bryant outdoor unit), `weather.forecast_home` (Met.no) | |
| Sun gain proxy | `sensor.envoy_202252012437_current_power_production` (kW) | Solar output stands in for sun on the house. |
| Occupancy | `binary_sensor.home_zone_1_occupancy` | |

HA units are °C. All entity ids above are defaults in the config flow, never hard-coded in logic.

## What the user sees

- `climate.house`: one thermostat. Target step 0.5 °C. Modes `off`, `heat`, `cool`, `heat_cool`
  (auto: the integration picks heat or cool). Current temperature = the house temperature.
  The standard thermostat card's +/− buttons are "warmer"/"colder".
- `button.house_warmer` and `button.house_colder`: change the house setpoint by ±0.5 °C.
- `sensor.house_temperature` (weighted mean of member sensors) and `sensor.house_temperature_spread`
  (max − min of member sensors).
- `sensor.house_<system>_offset` per system: the current offset from the house setpoint.
- `switch.house_climate_control`: when off, the integration observes and logs but sends no commands.
- Attributes on `climate.house` explain the last decision in plain words (`last_decision`), plus
  the per-sensor readings used.

## Configuration (config flow + options flow)

- Systems (climate entities), each with the list of sensors it mainly affects ("influence") in v1.
  Default: Bryant → all house sensors; Mitsubishi → the two living-room sensors.
- Member sensors, each with a weight (default 1.0) and an optional zone label. Adding a sensor
  later in the options flow is the "onboard a sensor" path; nothing else has to change.
- Outdoor temperature sensor, optional weather entity, optional solar power sensor, optional
  occupancy sensor.
- Tunables with defaults: control interval 5 min, mode-switch deadband 1.0 °C, minimum time in a
  mode 60 min, offset clamp ±3.0 °C, max offset change per interval 0.5 °C.

## Control loop (v1)

Every control interval, and immediately when the house setpoint or mode changes:

1. Read member sensors; drop unavailable or stale (>30 min) ones. House temperature = weighted
   mean of the rest.
2. Mode. If the house mode is `heat` or `cool`, use it. If it is `heat_cool`, pick heat when the
   house temperature is below setpoint − deadband, cool when above setpoint + deadband, otherwise
   keep the current mode; never switch sooner than the minimum time in a mode. Outdoor temperature
   vetoes nonsense: no cooling when outdoors is below 10 °C, no heating when above 24 °C
   (both tunable). Every system gets the same mode. `off` turns every system off.
3. Per-system offset, from a PI controller on the error of the sensors that system influences:
   - Bryant (whole house): error = setpoint − weighted mean of its influence sensors.
   - Mitsubishi (living room): error = setpoint − weighted mean of the living-room sensors.
   The Bryant pushes the whole house toward setpoint; the Mitsubishi trims the living room so the
   Bryant's living-room sensor does not stop it early. The effect is to warm the cold rooms
   without overheating the living room.
   Anti-windup, clamp to ±offset clamp, rate-limit per interval, round to 0.5 °C, and clamp the
   final setpoint to the device's min/max.
4. Write only what changed: `climate.set_hvac_mode`, then `climate.set_temperature`. For the
   Bryant, re-assert preset `manual` if it has left it.
5. If someone changes a system's setpoint or mode outside the integration, log it in
   `last_decision`; the next interval re-asserts (the House thermostat is the one control).
6. Fallbacks: if no member sensor is available, every system gets the house setpoint with zero
   offset. If a system is unavailable, skip it and keep controlling the others.

State (setpoint, mode, integrator values, last mode switch) survives restarts via `RestoreEntity`
/ `Store`.

## Learning (phase 2; v1 only records the data)

v1 records a sample every interval to a `Store` file: every member sensor, outdoor temperature,
solar power, occupancy, each system's mode, setpoint, offset and `hvac_action`. Capped to the last
30 days.

Phase 2 fits, per sensor i, a simple model by recursive least squares:
`dT_i/dt = a_i (T_out − T_i) + Σ_j b_ij · u_j + c_i · solar + d_i`, where u_j is system j's
running effort (from `hvac_action` and its offset). The learned b_ij replaces the hand-set
"influence" lists, and the offsets come from minimising the weighted spread of predicted room
temperatures subject to the house mean meeting the setpoint, within the same clamps. A newly
onboarded sensor counts in the house temperature at once and gets its b_ij after a few days of
data; until its fit is confident (tunable sample count), it uses the v1 influence list.

## Code layout

- `custom_components/house_climate/controller.py`: pure Python, no Home Assistant imports. Takes
  readings + config + state, returns the commands and the explanation. All the logic above lives
  here and is unit-tested directly.
- `climate.py`, `button.py`, `sensor.py`, `switch.py`, `config_flow.py`, `__init__.py`,
  `coordinator.py`: thin HA wiring around the controller.
- Tests: `tests/test_controller.py` (pure), plus HA tests with
  `pytest-homeassistant-custom-component` for setup, the config flow and the commands sent.

## Install

Private GitHub repo `Fledgewing/house-climate`. HACS does not support private repos, so the
integration is copied into the HA `/config/custom_components/` directory, then HA restarts.

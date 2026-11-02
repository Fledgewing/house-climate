# House Climate: design

A Home Assistant custom integration (`custom_components/house_climate`, domain `house_climate`)
that runs several HVAC systems as one house thermostat. It keeps them on the same heat/cool mode
and gives each system its own setpoint so the temperatures across the house stay close together.

## Example house

These generic entity IDs illustrate a possible installation; users select their own entities.

| Role | Example entity | Notes |
|---|---|---|
| Whole-house ducted system | `climate.ducted` | A system with a built-in living-room sensor and an optional required `manual` hold preset. |
| Living-room mini-split | `climate.living_room` | A second system that trims the living room. |
| Room sensors | `sensor.ducted_temperature`, `sensor.living_room_temperature`, `sensor.upstairs_temperature`, `sensor.downstairs_temperature`, `sensor.office_temperature`, `sensor.guest_room_temperature` | Member sensors across the house. |
| Outdoor | `sensor.outdoor_temperature`, `weather.home` | Optional temperature sensor and fallback weather source. |
| Sun gain proxy | `sensor.solar_power` (kW) | Solar output stands in for sun on the house. |
| Occupancy | `binary_sensor.occupancy` | Optional occupancy source. |

HA units are °C. The config flow starts with no entity IDs selected. Entity selectors filter
by domain and, for temperature, power and occupancy sources, device class.

## What the user sees

- `climate.house`: one thermostat. Target step 0.5 °C. Modes `off`, `heat`, `cool`, `heat_cool`
  (auto: the integration picks heat or cool). Current temperature = the house temperature.
  The standard thermostat card's +/− buttons are "warmer"/"colder".
- `button.house_warmer` and `button.house_colder`: change the house setpoint by ±0.5 °C.
- `sensor.house_temperature` (weighted mean of member sensors) and `sensor.house_temperature_spread`
  (max − min of member sensors).
- `sensor.house_learning` and `button.house_reset_learning`: model confidence, fitted room effects and reset.
- `sensor.house_<system>_offset` per system: the current offset from the house setpoint.
- `switch.house_climate_control`: when off, the integration observes and logs but sends no commands.
- Attributes on `climate.house` explain the last decision in plain words (`last_decision`), plus
  the per-sensor readings used.

## Configuration (config flow + options flow)

- Systems (climate entities), each with an optional required hold preset. Room effects are learned automatically.
- Member sensors, each with a weight (default 1.0) and an optional zone label. Adding a sensor
  later in the options flow is the "onboard a sensor" path; nothing else has to change.
- Outdoor temperature sensor, optional weather entity, optional solar power sensor, optional
  occupancy sensor.
- Tunables with defaults: control interval 5 min, mode-switch deadband 1.0 °C, minimum time in a
  mode 60 min, sensor stale after 720 min (12 h), offset clamp ±3.0 °C, max offset change per interval 0.5 °C.

## Control loop (v0.2.0)

Every control interval, and immediately when the house setpoint or mode changes:

1. Read member sensors; include finite numeric states unless unavailable/unknown. Battery and
   Z-Wave sensors may report only on changes, so age is only a backstop: drop readings older
   than `stale_after` minutes (default 720, editable in options), naming them in `last_decision`.
   Age uses `last_reported` (falling back to `last_updated`) so unchanged reports refresh it.
   House temperature = weighted mean of the rest.
2. Mode. If the house mode is `heat` or `cool`, use it. If it is `heat_cool`, pick heat when the
   house temperature is below setpoint − deadband, cool when above setpoint + deadband, otherwise
   keep the current mode; never switch sooner than the minimum time in a mode. Outdoor temperature
   vetoes nonsense: no cooling when outdoors is below 10 °C, no heating when above 24 °C
   (both tunable). Every system gets the same mode. `off` turns every system off.
3. Per-system offset, from a PI controller on the learned influence-weighted room error:
   `e_j = Σ_i w_i β_ij (S − T_i) / Σ_i w_i β_ij`. Here `w_i` is the configured sensor weight,
   and `β_ij` is the nonnegative blended heating/cooling coefficient, normalized per system.
   With neutral priors all systems start on the weighted house mean error. If all coefficients
   are near zero, or their rooms are unavailable, use the available house mean error.
   Each system pushes harder for the rooms it actually reaches, reducing spread while tracking
   the house target. `last_decision` lists the rooms driving each error, largest absolute
   weighted contribution first. During initial learning, add the exploration offset described below.
   Keep conditional integration/anti-windup, ±offset clamp, interval slew limit, 0.5 °C rounding,
   and device min/max target limits.
4. Write only what changed: `climate.set_hvac_mode`, then `climate.set_temperature`. For a system with a
   configured required preset, re-assert that preset if it has left it.
5. Track each system's last successfully commanded mode and target. Report an outside change
   in `last_decision` only when the reported mode/target differs from that command and more
   than 2 minutes have passed since it, suppressing delayed device echoes. The next interval
   re-asserts (the House thermostat is the one control).
6. Fallbacks: if no member sensor is available, every system gets the house setpoint with zero
   offset. If a system is unavailable, skip it and keep controlling the others.

State (setpoint, mode, integrator values, last mode switch) survives restarts via `RestoreEntity`
/ `Store`.

On a fresh setup with no restored state, adopt the available systems' shared heat, cool or
heat_cool mode and their mean target, rounded to 0.5 °C and clamped to the House's 7–35 °C
range (default 21 °C if no targets are available). If modes disagree, any available system is
off, or no systems are available, start in off mode with control disabled: observe without
sending commands until the user chooses a House mode. Explain this choice in `last_decision`.
Restored state takes precedence over adoption.

## Learning (v0.2.0)

Every scheduled interval fits one thermal model per available room sensor using recursive least
squares (RLS), in the pure `learning.py` module:

`ΔT_i = a_i (T_out − T_i) + Σ_j b_ij u_j + c_i solar_kW + d_i`.

Coefficients describe one configured control interval (5 minutes by default); elapsed time scales
regressors for delayed passes. `a_i` describes heat loss, `b_ij` the temperature response to each
system, `c_i` sun gain, and `d_i` background drift. Every system/room pair starts with the same
neutral `b = 0.03 °C/interval`. All other prior means are zero; diagonal prior covariance is 100
for every coefficient, with zero cross-covariances. There are no room-specific assumptions.

The signed effort `u_j` is actual heating seconds minus cooling seconds, divided by interval
seconds. A state-change listener integrates every `hvac_action` transition, including transitions
during command service calls. Idle/off/fan/drying contribute zero; unavailable or unknown actions
invalidate that interval. Sampling only the action at a pass would miss short cycles.

The forgetting factor defaults to 0.998 per valid row sample. Effective activity counts follow
`n_ij ← 0.998 n_ij + 1[u_j ≠ 0]`. Confidence is `n/(n+N0)` with `N0 = 24` by default (about two
hours of active 5-minute samples to give learned and prior coefficients equal weight).
Control blends `confidence × learned_b + (1 − confidence) × 0.03`, clips negative results to
zero, then normalizes per system. Learning starts affecting control from the first valid sample;
several hours of informative runtime can outweigh the prior, and forgetting supports continued
adaptation. Confidence measures effective runtime evidence, rather than a statistical certainty
that two always-synchronized systems have been identified separately.

To distinguish system effects, while a system's minimum room confidence is below 80%, add a
±0.5 °C square-wave offset of period two hours, with system k shifted by k/N of a period. Its
amplitude fades linearly to zero at 80% confidence. This stays inside the existing clamp and slew
limits, and `last_decision` says `exploring` while it is active. Off mode and observe-only control
send no exploration commands. Adding a room can restart exploration while its new row learns.

A missing/nonfinite/stale room reading at either interval endpoint skips only that room's sample.
Missing outdoor context or any system effort skips the whole interval. Configure an outdoor
sensor or fallback weather temperature for learning. Solar is zero when no solar source is
configured; a configured missing source or unsupported unit skips the interval. W, kW and MW are
converted to kW. Occupancy remains recorded context, outside this model. Immediate user requests
run control without adding training samples or resetting runtime accumulation.

The Store persists coefficients, full covariance, effective counts, residual errors and sample
counts alongside existing control state and the last 30 days of observations. Restarts retain
the fit but start a new observation interval, avoiding invented runtime across downtime. Adding
a sensor creates a neutral row immediately and retains existing rows; removing one deletes its
row. Adding/removing/reordering systems reconciles coefficient and covariance columns by entity
ID. Config-entry version 2 migrates existing data and options by removing obsolete room mappings,
retaining the other settings and existing Store. Pre-learning Store files start at neutral priors.

`sensor.house_learning` reports the mean confidence across all system/room pairs, as a percentage.
Its `systems[system_entity][sensor_entity]` attributes expose raw `b`, `blended_b`, normalized
`influence`, confidence percent and `effective_samples`. Its `sensors[sensor_entity]` attributes
expose `a` (heat loss), `c` (sun gain), `d`, `fit_error` (forgetting-weighted RMS prediction error
in °C), and `samples` (valid updates). Press `button.house_reset_learning` to reset the model and
confidence to neutral priors; house target, mode and PI control state remain in place.

The seeded two-day test uses two systems and six rooms, a nonuniform response matrix, outdoor
5 °C with a day/night swing, solar gain, and cycling thermostats with the real controller in the
loop. It checks every learned coefficient within 0.003 °C/interval of truth, house mean within
0.5 °C throughout day two, spread improvement against learning disabled, useful learning within
hours, and exploration active early but absent at the end.

## Code layout

- `custom_components/house_climate/controller.py`: pure Python, no Home Assistant imports. Takes
  readings + config + state, returns the commands and the explanation. All the logic above lives
  here and is unit-tested directly.
- `learning.py`: pure online RLS, confidence blending, runtime integration and model persistence.
- `climate.py`, `button.py`, `sensor.py`, `switch.py`, `config_flow.py`, `__init__.py`,
  `coordinator.py`: thin HA wiring around the controller.
- Tests: `tests/test_controller.py` (pure), plus HA tests with
  `pytest-homeassistant-custom-component` for setup, the config flow and the commands sent.

## Install

Public GitHub repository `Fledgewing/house-climate`. In HACS → Custom repositories, add
`https://github.com/Fledgewing/house-climate` as an Integration, download, restart Home Assistant,
then add the House Climate integration. Alternatively, copy `custom_components/house_climate`
into `/config/custom_components/` and restart before adding the integration.

## 0.1.2 fixes

Replaced the 30-minute sensor cutoff with the configurable 12-hour backstop to keep healthy,
quiet room sensors in whole-house control. Delayed HVAC command echoes no longer produce
false outside-change notes; mismatches have a two-minute grace period.

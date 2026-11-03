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
- `select.house_priority_mode`: named room priorities, persisted and available to HA automations.
- `button.house_warmer` and `button.house_colder`: change the house setpoint by ±0.5 °C.
- `sensor.house_temperature` (weighted mean of member sensors) and `sensor.house_temperature_spread`
  (max − min of member sensors).
- `sensor.house_learning` and `button.house_reset_learning`: model confidence, fitted room effects and reset.
- `sensor.house_<system>_offset` per system: the current offset from the house setpoint.
- `switch.house_climate_control`: when off, the integration observes and logs but sends no commands, including fan/vane commands.
- `switch.house_fan_control` (default on): when off, leave fan/vane settings alone.
- `sensor.house_fan_learning`: confidence percent, per-context option rewards/counts and best fan/vane settings.
- Attributes on `climate.house` explain the last decision in plain words (`last_decision`), plus
  the per-sensor readings used.

## Configuration (config flow + options flow)

- Systems (climate entities), each with an optional required hold preset. Room effects are learned automatically.
- Member sensors, each with a weight (default 1.0) and an optional zone label. Adding a sensor
  later in the options flow is the "onboard a sensor" path; nothing else has to change.
- Outdoor temperature sensor, optional weather entity, optional solar power sensor, optional
  occupancy sensor.
- Tunables with defaults: control interval 5 min, mode-switch deadband 1.0 °C, minimum time in a
  mode 60 min, sensor stale after 720 min (12 h), frozen after 4 h, median window `filter_minutes` 15 min, maximum sensor rate
  `max_rate` 0.5 °C/min, offset clamp ±3.0 °C, max offset change per interval 0.5 °C.

## Control loop (v0.4.0)

Every control interval, and immediately when the house setpoint or mode changes:

1. Read member sensors; include finite numeric states unless unavailable/unknown. Battery and
   Z-Wave sensors may report only on changes, so age is only a backstop: drop readings older
   than `stale_after` minutes (default 720, editable in options), naming them in `last_decision`.
   Age uses `last_reported` (falling back to `last_updated`) so unchanged reports refresh it.
   Independently, persist each sensor's numeric value and last real value-change time;
   unchanged reports, attribute changes, unavailable markers and HA restarts do not reset it.
   A value unchanged for `frozen_after` hours (default 4, editable in options) with at least
   one hour of observed heating/cooling in that window is "not reporting". Count the union
   of actual `hvac_action` activity across systems, including action changes between ticks;
   idle/off time and unobserved downtime do not count. Once frozen, exclude the sensor until
   its first numeric value change, which triggers recovery immediately.
   At startup, seed from retained recorder history when available: compare numeric values
   across restart markers to find the last actual change, plus action history for the activity
   window. If retention has no earlier different value, the earliest retained observation is
   a conservative lower bound on unchanged age. Without history, use Store evidence or start
   observing now.
   Optionally resolve timestamp entities ending in `_last_seen` on the member's registered
   device. A timestamp older than `stale_after` independently excludes that member; no device
   protocol or entity IDs are assumed. This signal clears when the timestamp becomes fresh.
   Raise a warning repair per not-reporting member, naming the sensor and last real change
   and asking the user to check its battery; clear the repair on recovery. Expose the same
   details in `climate.house.not_reporting` and `last_decision`.
   Each member has a generic rolling, time-weighted median over `filter_minutes`
   (default 15 minutes); readings hold their value until the next state change.
   Capture every sensor state-change event, including events between control passes.
   Unknown/unavailable periods carry no median weight and remain unavailable at the
   current endpoint. Control, house mean/spread and learning all use these medians;
   the existing reporting/staleness checks still apply to the raw member.
   A movement faster than `max_rate` (default 0.5 °C/min) opens a pending excursion,
   immediately withholding its readings from control and learning. Returning to within
   one minute's allowed movement of the pre-excursion value, or crossing that value,
   within the filter window confirms a transient. Exclude its held intervals from the
   median; name the member in `last_decision` as “sensor transient ignored”. If it
   does not return, admit the sustained change when the window expires, evaluated
   on every control pass even if the sensor sends no further reports. While all
   elapsed readings are withheld, hold the pre-excursion value. This is symmetric
   for upward/downward excursions and assumes no device type or operating cycle.
   Skip learning rows whose interval overlaps a pending or confirmed excursion,
   including an excursion that starts and ends between passes. Require clean endpoints
   before resuming. Keep only the rolling event window plus its preceding value,
   rejected intervals and pending anchor in Store for restart continuity; do not
   archive event history. Restarts still start a fresh learning/runtime interval.
   House temperature = weighted mean of the remaining filtered reporting sensors.
   `climate.house.raw_sensor_readings` exposes raw value, age, state and reporting
   status alongside `filtered_sensor_readings` (also the existing `sensor_readings`).
2. Mode. If the house mode is `heat` or `cool`, use it. If it is `heat_cool`, pick heat when the
   house temperature is below setpoint − deadband, cool when above setpoint + deadband, otherwise
   keep the current mode; never switch sooner than the minimum time in a mode. Outdoor temperature
   vetoes nonsense: no cooling when outdoors is below 10 °C, no heating when above 24 °C
   (both tunable). Every system gets the same mode. `off` turns every system off.
3. Per-system offset, from a PI controller on the learned influence-weighted room error:
   `e_j = Σ_i w_i β_ij (S − T_i) / Σ_i w_i β_ij`. Here `w_i` is the active priority mode's sensor weight,
   and `β_ij` is the nonnegative blended heating/cooling coefficient, normalized per system.
   With neutral priors all systems start on the weighted house mean error. If all coefficients
   are near zero, or their rooms are unavailable, use the available house mean error.
   Each system pushes harder for the rooms it actually reaches, reducing spread while tracking
   the house target. `last_decision` lists the rooms driving each error, largest absolute
   weighted contribution first. During initial learning, add the exploration offset described below.
   Keep conditional integration/anti-windup, ±offset clamp, interval slew limit, 0.5 °C rounding,
   and device min/max target limits.
4. Run the fan/vane scheduler described below, adding at most one changed fan/vane command.
   Write only what changed: `climate.set_hvac_mode`, then `climate.set_temperature`. For a system with a
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

A missing/nonfinite/stale/not-reporting room reading at either interval endpoint skips only that room's sample. Excluded rows retain all coefficients, covariance, counts and fit errors unchanged; their confidence also does not drive control exploration.
Missing outdoor context or any system effort skips the whole interval. Configure an outdoor
sensor or fallback weather temperature for learning. Solar is zero when no solar source is
configured; a configured missing source or unsupported unit skips the interval. W, kW and MW are
converted to kW. Occupancy remains recorded context, outside this model. Immediate user requests
run control without adding training samples or resetting runtime accumulation.

The Store persists coefficients, full covariance, effective counts, residual errors and sample
counts alongside persisted value-change clocks, frozen status, observed activity intervals, existing control state and the last 30 days of observations. Restarts retain
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

## Fan/vane learning (v0.4.0)

Pure `fans.py` implements a contextual UCB bandit per system and per action (`fan`, `swing`).
Action options are read from each climate entity's `fan_modes` and `swing_modes` on every pass;
missing/empty lists offer no action. The comma-separated options field `learn_exclude_modes`
(default `auto`) excludes matching names case-insensitively from both actions, trimming whitespace
and ignoring empty entries. Auto lets the unit choose its speed/position, so its mixed reward
cannot teach the bandit a specific setting. All other options come from the entity; no preferred
speeds/positions are encoded. An action with every mode excluded is left alone and has no arms.
A current excluded mode is moved to a learnable arm at the next scheduler decision, counting as
that hold window's one change. Loading persisted learning drops excluded arms, choices and active
experiment identities, retaining other experience.
All options start with mean reward zero and sample count zero. Untried arms are selected with
random tie breaks; thereafter select the largest `mean_reward + sqrt(2 ln(N+1)/n)`, with random
score ties. Systems/actions take turns through one global experiment scheduler.

Context keys encode heat/cool, `floor(outdoor/5)*5` (including negative bands), and whether that
system is heating/cooling. Unknown outdoor temperature offers no context. On each observation, retain each clean filtered room's error
`e_i = T_i − S` until the next pass. Integrate error and squared error over observed seconds
in the full hold. Each arm stores `rooms[sensor] = {seconds, mean_error, mean_squared_error}`,
merging clean windows by duration, plus its window sample count. As before, the fan learner
has no forgetting. At selection and diagnostics time, compute current normalized priority weights
among the arm's observed member rooms and score:

`r = −(Σ w_i E[e_i²] − (Σ w_i E[e_i])²) − λ (Σ w_i E[e_i])²`.

`fan_reward_lambda` is λ, default 1 (options: 0–100). This is the variance/mean-error reward
expressed as room moments, so historical evidence can be reweighted without resetting it.
Removed rooms do not contribute to scores. Legacy scalar-only arms load with empty room
statistics and zero sample counts; other scheduler identities survive. `mean_reward` is a derived
cache, recomputed under the current weights rather than used as permanent learning evidence.
Hold the selected setting for at least `fan_hold_minutes` (default 30, options: 1–1440). Selecting an already-current option sends nothing but still starts a full experiment.
Only one action of one system can be commanded in a hold window; other chosen settings remain
fixed. The PI temperature controller continues tracking the house setpoint throughout.

A sensor transient or missing/stale/not-reporting member with at least
`minor_weight_share` of the total active-mode positive sensor weight (default 0.05),
an HVAC mode switch, user house/physical
setpoint change or selected-context change dirties the complete experiment. Events between ticks
latch dirtiness; recovery before a tick cannot make it train. Dirty windows retain their deadline
but contribute no sample. Disabled house/fan control invalidates experiments and sends no fan/vane
commands. Blocked minor members are still excluded from fan rewards and their
thermal learning rows, while healthy rooms continue training. Commands use `climate.set_fan_mode` / `climate.set_swing_mode` only when different.
Failures discard the window. Re-assert outside changes to chosen settings, restarting a full
hold; another action's re-assertion waits for the global deadline. Outside-change notices use the
same two-minute echo grace as targets. The required preset path remains unchanged.

Store retains per-room error moments and window counts by context, chosen settings, scheduler cursor and active
experiment identity, plus the fan-control switch. Restarts keep the fit but restart a full hold,
never crediting unobserved downtime. Removed systems are pruned; new options start neutral.
The existing reset-learning button resets this learner too. `sensor.house_fan_learning` attributes
are `systems[entity][fan/swing][context]` with sampled `best` and `options[mode]` containing
`mean_reward`, `samples` and `rooms`, plus `last_decision`. Unobserved contexts have no best. Confidence
is mean `100*n/(n+10)` across discovered contexts, where n is the minimum option count in that
context: an evidence-coverage indicator, not statistical certainty. The thermostat's `last_decision`
also explains trying a setting to learn, the best so far with heat/cool and outdoor band, or an
outside-change re-assertion. Human-readable device names are used for exploration/best notes.

Quiet hours are **disabled by default** (`quiet_hours_enabled=false`), so no initial restrictions
apply. Options provide `quiet_start=22:00`, `quiet_end=07:00`, and each system's optional
`quiet_max_fan`, selected from its runtime `fan_modes`. Use HA local time, start inclusive/end
exclusive, supporting midnight crossings; equal start/end means no quiet window. Inside the
window only options up to the maximum's list index are eligible, with no exception for `auto`.
Outside it, when disabled, or without a matching maximum, all learnable advertised options remain
eligible. Quiet restrictions change candidate lists, never their reward priors; vanes are
unrestricted. If the current fan becomes excluded, cap it on the first pass inside the quiet
window, interrupting the global hold and discarding its sample. Start a fresh full hold
when context is available, retaining the one-action-per-pass rule. The cap also applies
when sensor or outdoor context prevents learning.

Regression tests cover neutral/exploring arms, context buckets, weighted reward/λ, global holds,
outside changes, quiet hours on/off and midnight crossings, skipped windows and persistence.
A seeded three-fan physical simulation runs 600 half-hour experiments with five-minute physical
steps: it learns the option whose equilibrium spread is 0.6 °C rather than 4.0 °C, selects it for
100% of the final 100 windows, and keeps mean error below 0.008 °C. HA tests exercise actual service
dispatch, both switches, options selectors, between-pass contamination and Store reloads.

## Code layout

- `custom_components/house_climate/controller.py`: pure Python, no Home Assistant imports. Takes
  readings + config + state, returns the commands and the explanation. Temperature control logic lives
  here and is unit-tested directly.
- `fans.py`: pure contextual bandit, global fan/vane holds and quiet-hour eligibility.
- `learning.py`: pure online RLS, confidence blending, runtime integration and model persistence.
- `reporting.py`: pure persisted value-change and HVAC activity tracking for frozen sensors.
- `filtering.py`: pure bounded time-weighted median and generic transient rejection.
- `priority.py`: mode membership reconciliation and active room weights.
- `climate.py`, `button.py`, `sensor.py`, `switch.py`, `select.py`, `config_flow.py`, `__init__.py`,
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

## 0.2.1 fixes

Exclude frozen sensors after four unchanged hours with an hour of actual HVAC activity,
with persisted clocks and recorder startup seeding. Optional device last-seen timestamps
catch stale devices even when HA refreshes state metadata. Repairs and thermostat diagnostics
identify affected sensors; control and learning use only reporting rooms. Regression coverage
includes five-hour freezes, idle systems, recovery, persistence, recorder seeding, stale device
timestamps and unchanged excluded learning rows.

## 0.2.2 fixes

Generic per-sensor time-weighted medians and rate-based transient rejection protect both
control and learning from brief sensor excursions. Added the `filter_minutes` and
`max_rate` options, raw/filtered thermostat diagnostics and bounded restart recovery.
Regression coverage replays the November 2 real-house cold excursion and warm recovery,
checks stable house temperature and no split target increase, skips contaminated learning
rows, and tracks a genuine 0.1 °C/min hour-long drop with less than 15 minutes of lag.


## 0.3.1 fixes

The first control pass and runtime listeners wait for `EVENT_HOMEASSISTANT_STARTED`;
config-entry reloads while HA is fully running start immediately. The first numeric
reading in each new runtime establishes a fresh filter level if it differs from Store,
so startup unavailable-to-value transitions cannot invent rate excursions. Existing
same-level filtering evidence remains bounded and retained.

Report-on-change steps expire at `filter_minutes` by wall-clock time without more
reports; control passes evaluate held readings without creating synthetic samples.
Minor members below the configurable 5% weight share cannot dirty fan experiments,
but blocked/missing/not-reporting members contribute no reward or thermal learning row.
Quiet-hour caps interrupt holds immediately on the first pass in the window.

Per-system offsets and PI integrals were already serialized in the control state.
Deferring startup avoids the no-sensor fallback resetting them before integrations load.
Regression tests round-trip distinct offsets/integrals for both systems through Store,
startup and reload, and cover silent permanent steps, repeated minor transients,
missing/not-reporting minor members and immediate quiet caps.


## Priority modes (v0.4.0)

Entry options hold `priority_modes`, a mapping from mode name to sensor-ID/weight mapping.
Config-entry version 3 migrates the old per-sensor weights into one `balance` mode, preserving
control output and all RLS state. New setups also start with balance. The options wizard's
priority action adds, edits, renames or deletes modes, protecting the last mode. Ordinary
sensor weight editing edits the active priority mode; explicit edit selects another mode.
New modes start at 1 per member. Membership reconciliation adds new sensors at weight 1 to
all modes and drops removed sensors from all modes. Legacy member weights remain compatible
configuration metadata; all runtime weighted paths use the selected mode's mapping.

`select.house_priority_mode` exposes names as options and persists the active name in Store.
Selection runs an immediate non-training control pass, preserving RLS coefficients, covariance,
counts, residuals and sampling baseline. RLS fits raw room dynamics independently of priorities.
House mean, eligible spread, per-system PI error, fan scoring and `minor_weight_share` checks
use the active weights. `climate.house.priority_mode`, `priority_weights` and `last_decision`
expose the choice; its existing `active_mode` continues to mean HVAC heat/cool mode.
Renaming preserves the active selection through a saved name mapping; deleting the active mode
falls back to the first remaining mode. No built-in scheduler is added: ordinary HA automations
call `select.select_option` (README includes time triggers). An override is another selection.

Regression coverage checks v1/v2 migration and identical control commands, immediate select
service effects on mean and both system errors with untouched learned state, Store and actual
entry reloads, mode CRUD and last-mode protection, active weight editing, sensor membership,
legacy scalar fan loading, time-weighted error moments and reversed fan ranking under two
room priorities.

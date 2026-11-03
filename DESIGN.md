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
- `switch.house_learning` (default on): pause thermal and fan learning, with an optional persisted auto-resume deadline.
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
  `max_rate` 0.5 °C/min, prediction horizon 45 min, own-sensor `run_margin` 1 °C,
  effort hysteresis 0.05 and effort on/off dwell 5 min. Legacy offset clamp, slew and PI
  gains remain readable for compatibility but no longer govern commands.

## Control loop (v0.5.2)

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
3. Joint allocation. The House setpoint S is the weighted house **average**. Minimize
   each room's deviation from that average and the average's error from S together:

   `J(u) = Σ_i w_i (T_hat_i − T_hat_mean)² + λ (T_hat_mean − S)²`.

   Normalize the active positive room weights to sum to one. Reuse `fan_reward_lambda`
   for λ (default 1). `learning.forecast` propagates each interval model
   `ΔT_i = a_i(T_out − T_i) + Σ_j b_ij u_j + Σ_k e_ik source_k + c_i solar + d_i`
   to the horizon (default 45 minutes), holding current outdoor, source states and solar
   constant. Propagate losses every step; the resulting forecast is affine in effort.
   Missing context omits that term. All nonnegative b coefficients use the existing
   optimistic confidence blend, preserving their physical scale rather than normalizing
   each system's room effects. Without learning, use the same neutral b prior for every pair.
   No room mappings, individual room targets or overshoot ceilings enter the objective.

   Solve this small convex box problem using up to 200 cyclic exact coordinate minimizations,
   stopping when effort changes fall below 1e-9. Heat efforts are 0..1; cool efforts −1..0.
   Unavailable systems have no variable. With no learned positive reach, an actuator gets
   zero effort instead of inventing reach from a cold room's error.

   A single persisted mean-error integral biases S by at most ±1 °C. Its separate
   `mean_integral_gain` defaults to 0.001 per scheduled interval; immediate requests do not
   integrate. Conditional integration stops increasing the bias when the predicted mean
   cannot follow it in the requested direction (0.05 °C² tolerance). Old per-system PI
   integrals remain loadable but are unused. This keeps upgrades from applying old PI
   gains or room errors to the joint objective.

   Translate effort magnitude q to `own_current_temperature + direction × run_margin × (2q−1)`.
   Thus full heat commands the system's own reading +1 °C and zero heat its own reading −1 °C;
   cooling mirrors this. Intermediate efforts provide proportional targets for modulating
   units. If the own sensor is missing, explicitly report using the house mean as a proxy.
   Retain effort hysteresis and minimum on/off dwell (defaults 0.05 and 5 min). A scheduled
   pass can cross the own sensor in either direction regardless of old house offsets or slew.
   Round to the advertised target step (fallback 0.5 °C) and clamp to device min/max.
   Offsets remain diagnostics: actual commanded target minus the house setpoint.

   Cycling thermostats may deliver a different duty fraction from the proportional target.
   Close that actuator loop using the previous requested effort and measured action runtime:
   adjust a persisted drive bias by 0.1 times their difference, bounded to ±0.5 effort.
   Apply it only to intermediate efforts; full on/off endpoints stay intact. Unknown runtime,
   immediate requests and held excitation do not train it. Carry the bounded target-rounding
   remainder into later scheduled passes so coarse target steps do not create a permanent
   effort bias. Repeated immediate calls reuse the same rounding adjustment. A held excitation
   keeps its exact target and suspends this remainder until normal allocation resumes.
   Normal dither and inactivity excitation still apply, bounded in effort/device space.

   `last_decision` names each system's on/low effort, the rooms its learned reach can lift/cool
   toward the average, and the room above/below that average driving restraint. Room and device
   friendly names are used when available; the implementation contains no house-specific names.
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

`ΔT_i = a_i (T_out − T_i) + Σ_j b_ij u_j + Σ_k e_ik s_k + c_i solar_kW + d_i`.

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
Control blends `confidence × learned_b + (1 − confidence) × system_prior` and clips negative results to
zero. Joint prediction uses those physical coefficients directly; normalized per-system influences remain diagnostic. `system_prior` is the confidence-weighted mean raw learned b
over that system's sensors with nonzero runtime confidence, falling back to 0.03 only when
none has evidence. Unexplored pairs therefore inherit measured system reach rather than
being mistaken for weak influence. Learning starts affecting control from the first valid sample;
several hours of informative runtime can outweigh the prior, and forgetting supports continued
adaptation. Confidence measures effective runtime evidence, rather than a statistical certainty
that two always-synchronized systems have been identified separately.

To distinguish system effects, while a system's minimum room confidence is below `explore_threshold` (default 0.8), add a
±0.5 °C square-wave offset of period two hours, with system k shifted by k/N of a period. Its
amplitude fades linearly to zero at the confidence threshold. It perturbs proportional effort within
0..1 magnitude and device limits, and `last_decision` says `exploring` while it is active. Off mode and observe-only control
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
confidence to neutral priors; house target, mode and control state remain in place.

The seeded two-day test uses two systems and six rooms, a nonuniform response matrix, outdoor
5 °C with a day/night swing, solar gain, and cycling thermostats with the real controller in the
loop. It checks every learned coefficient within 0.003 °C/interval of truth, house mean within
0.5 °C throughout day two, spread improvement against learning disabled, useful learning within
hours, and exploration active early but absent at the end.

## Fan/vane learning (v0.4.1)

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
fixed. The joint temperature allocator continues tracking the house setpoint throughout.

A missing/stale/not-reporting member with at least
`minor_weight_share` of the total active-mode positive sensor weight (default 0.05),
an HVAC mode switch, user house/physical
setpoint change or fan-control toggle dirties the complete experiment. Rejected/pending
transients do not: fan rewards use the robust filtered value, including its held anchor.
Outdoor band and runtime context changes apply to the next selection; each hold retains
its starting context. Events between ticks
latch dirtiness; recovery before a tick cannot make it train. Dirty windows retain their deadline
but contribute no sample. Disabled house/fan control invalidates experiments and sends no fan/vane
commands. Missing/stale/not-reporting minor members are excluded from fan rewards;
transients still block their thermal learning rows while fan rewards use filtered values. Commands use `climate.set_fan_mode` / `climate.set_swing_mode` only when different.
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
House mean, eligible spread, joint thermal allocation, fan scoring and `minor_weight_share` checks
use the active weights. `climate.house.priority_mode`, `priority_weights` and `last_decision`
expose the choice; its existing `active_mode` continues to mean HVAC heat/cool mode.
Renaming preserves the active selection through a saved name mapping; deleting the active mode
falls back to the first remaining mode. No built-in scheduler is added: ordinary HA automations
call `select.select_option` (README includes time triggers). An override is another selection.

Regression coverage checks v1/v2 migration and identical control commands, immediate select
service effects on mean and joint allocation with untouched learned state, Store and actual
entry reloads, mode CRUD and last-mode protection, active weight editing, sensor membership,
legacy scalar fan loading, time-weighted error moments and reversed fan ranking under two
room priorities.


## 0.4.1 fixes

Fan windows now use reporting filtered room values even through pending or rejected
transients. Previously, both the sensor event listener and `fan_clean` treated filter
rejection as invalidation, starving experiments on a significant-weight defrost sensor.
The invalid/context early return also bypassed excluded-mode correction and left
`last_decision` at its startup message indefinitely across restarts.

Excluded-mode correction, chosen-setting re-assertion and quiet-hour caps run before
learning eligibility checks. Corrections preserve the global hold rule (quiet caps
interrupt it), send at most one changed setting per pass, and collect no invalid reward.
Each paused return states its cause, including sensor IDs and missing outdoor readings;
fan sensor diagnostics expose `wait_reason` and the active `window` with its dirty reason.
Track in-flight temperature commands so target adjustments do not masquerade as user changes.

Regression coverage reuses the v0.2.2 minute replay every 40 minutes for four hours with
a 0.5-weight member above `minor_weight_share`, completing eight fan windows despite
repeated defrost-shaped dips. It also checks startup auto correction with invalid room
readings or missing outdoor context, re-assertions, quiet caps, concrete pause reasons,
and continued rejection of real missing/not-reporting/mode/setpoint contamination.


## 0.4.2 learning fix

Low-confidence room/system pairs blend toward that system's confidence-weighted mean learned
b across explored rooms (any nonzero confidence), with the neutral 0.03 fallback only when
nothing is explored. This uses measured effects without configured room mappings.

Dither alone cannot start a system whose local sensor is far above its target. If any available,
positive-weight member has confidence below `explore_threshold` (default 0.8), and a system has
not actually heated/cooled for `excite_after_minutes` (default 120), start one global excitation.
The weighted house mean must be below setpoint in heat or above in cool. Set the target to the
system's own `current_temperature` plus 1 °C in heat or minus 1 °C in cool, rounded to 0.5 °C
and bounded by device limits. In v0.5.2 the margin follows `run_margin`, and house-relative
clamps/slew no longer constrain any effort target. Hold the excitation target while other
systems continue joint control.

Hold until `excite_minutes` (default 30) of reported matching heating/cooling action have
accumulated, or 60 wall-clock minutes pass. Action transitions between ticks count; idle,
unknown and unavailable time do not. Abort on control off, mode change, unavailable system,
missing house readings, or the house reaching/passing setpoint. A completed/aborted experiment
waits another inactivity interval before retrying that system, allowing other systems a turn.
Normal joint allocation then resumes. Names in `last_decision` read
“exploring: running <name> to learn which rooms it reaches”. All three tunables are in options;
setting the threshold to zero disables thermal exploration, including dither.

Store retains inactivity and retry clocks. On restart discard an active experiment and begin
its cooldown, clearing action endpoints so downtime never becomes observed runtime. Systems
without a finite local temperature cannot start a forced run. Clamps can limit the achievable
excitation target; the one-hour deadline bounds unsuccessful attempts.

The deterministic two-day regression starts at 24 °C in two explored living-room sensors and
18.8 °C in two newly joined cold sensors (weights 1, 1, 2, 2; weighted mean 20.53 °C), setpoint
22.5 °C and outdoor 7 °C. B's local sensor reads the living room; A's local sensor is 4.5 °C
cooler, so A can keep running at its backed-off target while B stays idle. Both systems' warm
sensor counts start at 80; the cold rows start with zero runtime evidence. B last ran 14 hours
ago. Cold-room losses balance A's small effects near the initial temperatures; B has strong
but initially unobserved effects. Minute thermostat cycles feed actual runtime to the real
controller and RLS learner at five-minute intervals. The comparison retains v0.4.1 learning
and dither but disables excitation and restores the fixed prior blend. Assert B runs within
three hours, learns larger cold-room b than A, and lowers final and day-two average weighted
standard deviation (the simulation's weighted spread metric). Production spread remains max−min.


## External inputs and robust learning (v0.5.0)

`extra_heat_sources` is an optional list of `{entity_id, kind, threshold}` records in
entry data/options. The ordinary membership page selects any binary-like or numeric
entity; each source page chooses `binary` (literal HA on/off states) or `power`
(finite W, kW or MW, strictly above `threshold` W, default 10). No heat-source device
names or room mappings appear in implementation. Sources are never commanded.

RLS columns are `[outdoor−room, HVAC efforts..., source fractions..., solar, 1]`.
Source coefficients have zero prior means, covariance diagonal 100 and zero initial
cross-covariances. Source effective activity counts use the same forgetting and
`n/(n+N0)` confidence as HVAC inputs. Sources use a separate `EffortTracker`, translating
on/off to 1/0, and listen to every state transition even during command locks. Both
trackers finish at the same scheduled passes; immediate control requests leave their
integrals intact. Unknown observations make the interval unavailable. Observations
record the integrated fractions. Startup and pause/resume reset the runtime baseline,
so no unobserved interval is invented.

Reconciliation maps covariance and coefficients by `(input kind, entity ID)`, retaining
all surviving cross-covariances, fit errors, sample counts and effective activity counts.
New columns alone start neutral; source removal shrinks rows without resetting HVAC,
solar or drift. Old Store models with no source metadata load unchanged.
`sensor.house_learning.sources[source][room]` exposes `e`, confidence percent and
`effective_samples`; overall confidence remains the HVAC-pair mean used previously.

For each available row interval, calculate the prediction residual before updating.
Normalize residual and fit error to one configured interval; delayed intervals scale
the bound by elapsed/configured duration. Bound is
`max(0.3, disturbance_sigma × fit_error)` °C per interval. Default sigma is 3.
The sensor's confidence is the mean across its HVAC pairs; below 50%, reset detection
streaks and never flag. An active configured source must also have at least `N0`
effective on samples (50% source confidence) before detection can flag its room: this
allows a new zero-prior column to learn a strong effect without becoming quarantined.

Two consecutive out-of-bound residuals in the same direction (configurable
`disturbance_intervals`) mark that row disturbed. Missing/invalid intervals reset
streaks; opposite-direction residuals start a new streak. While disturbed, coefficients,
covariance, counts, samples and fit error stay unchanged, but predictions continue for
recovery. Three consecutive within-bound intervals (`disturbance_clear_intervals`)
clear the flag; the clearing interval can train. Before every accepted update, clip
innovation to the bound (Huber-style). Update the RMS fit error using the clipped,
duration-normalized residual, preventing one outlier from expanding its own future
acceptance bounds. Sigma 0 disables both guards for comparison or troubleshooting.
Detection streaks/flags restart fresh after process restart; the fitted model persists.

Control still sees disturbed filtered temperatures. Thermal skipping is per row.
The fan scheduler excludes disturbed room rewards and invalidates a complete window
only if that member's active-priority share is at least `minor_weight_share`.
Modeled source operation alone never dirties windows. Thermostat attributes expose
`disturbed` as room entity to heat/cooling direction and `last_decision` uses friendly
room names to explain skipped learning.

`switch.house_learning` has a distinct unique ID from the existing learning sensor.
Off blocks all RLS updates and fan reward updates while ordinary control/corrections
continue. `learning_pause_hours` defaults to 0 (indefinite); positive values create an
absolute UTC deadline. Store persists enabled state and deadline; the first pass at or
after it resumes automatically, including after downtime. Toggle and auto-resume discard
partial thermal intervals and fan holds, preserving all fitted evidence. Status is
exposed on the thermostat and the switch. Reset-learning does not silently unpause.

The v0.4.2 Store already serializes `ControlState.last_run`; v0.5.0 fills the missing
startup evidence path. Recorder action history is read across retained history rather
than only the frozen-sensor window. For systems without a stored clock, seed the end
of the last observed heating/cooling interval before `control()` can use `setdefault`.
A running history tail with no following transition supplies only its last retained
observation timestamp, not invented runtime through downtime.
Stored clocks take precedence. Reporting activity still clips to `frozen_after`, so
old action history cannot freeze a room today. No history means observing from startup;
active excitation is canceled on restore and downtime earns no runtime.

Regression coverage includes source onboarding, power unit/threshold handling,
between-pass source events under lock, missing sources, Store and options reconciliation,
positive/negative two-hour single-room disturbances, same-direction and clear streaks,
cold-start suppression, clipped outliers, minor/major fan invalidation, modeled heat
with six valid thermal samples and a completed fan hold, indefinite/timed pause across
reload, fresh resume baselines, and recorder seeding from a run 14 hours earlier.

The seeded ten-day physical simulation has two HVAC systems and three rooms, independent
weather/solar variation, random duty fractions correlated with evening heat, room losses
and small measurement noise. A source heats two rooms at 0.85/0.70 °C per full-on interval,
with no effect on the third. After one clean commissioning day it runs on nine random
evenings for 295 intervals. Mean absolute HVAC b errors: modeled input 0.000144,
unmodeled with detection 0.005379, unmodeled without protection 0.227647 °C/interval.
Every modeled b is within 0.003 of truth; source e estimates are
0.850051/0.699994/−0.000098 (tolerance 0.005). Detection flags both heated rooms for
286/295 source-on intervals, flags the unaffected room zero times, and leaves it
learning for all 2880 intervals. The modeled case has zero flags. Protection reduces
omitted-source HVAC coefficient error by about 42 times in this simulation.

## Room regimes (v0.5.1)

Each member now owns an active RLS row, an independent challenger and a library of
up to three frozen rows. Control and disturbance detection always use the active
row. The challenger trains on every valid room interval, including disturbed
intervals; missing/filter-rejected data and the global learning pause still skip
training. Its forgetting factor is `exp(-elapsed_minutes / 1440)`, an effective
24-hour memory independent of the configured interval. The active row retains
ordinary slow forgetting and the existing disturbance guards. The disturbance
floor now decreases with runtime confidence: `max(0.1, 0.3 × (1 − confidence))`,
still bounded below by `disturbance_sigma × fit RMS`. Active configured sources
with fewer than N0 effective samples retain the 0.3 °C commissioning floor.
This protects established models from smaller persistent changes without an age
or days threshold; cold-start and new-source learning retain their allowance.

To identify abrupt changes promptly, two challenger innovations exceeding
`max(0.1, 6 × active fit RMS)` restore its neutral prior covariance after at least
24 training samples. Its coefficients, counts and errors remain intact. This
uncertainty boost starts a fresh identification evidence period; automatic new-model
promotion needs `regime_hours` of stability after it. Repeated evening events
therefore cannot accumulate evidence as though they were one continuous change.

Score predictions **before** training using duration-normalized absolute error,
weighted by observed seconds, in UTC-aligned two-hour blocks. Intervals crossing
boundaries contribute proportionally to each block. Only completed blocks in the
latest evidence window count; every block must have at least 90% observed coverage.
Unobserved time, including restart downtime, supplies no score. New-model promotion
requires a reduction strictly greater than `regime_margin` (default 0.30) in at
least 80% of blocks over `regime_hours` (default 24). Winning blocks must cover
local day (06:00–18:00), night, and each system both running and idle. The 80% win
requirement also applies within each of those contexts, preventing a few nighttime
wins or losses from being hidden by the daytime majority. A negligible active
error (≤0.00001 °C/interval) cannot establish a win. Evidence horizons round up
to complete two-hour blocks.

Library rows receive identical pre-update scores but never train. A proven saved
row can return after `regime_return_hours` (default 4), with the same error margin
and 80% block requirement; it need not repeat the day/night commissioning variety.
Saved rows that qualify take precedence over a new challenger. On every switch,
archive the old active row with its complete covariance, counts and error history;
activate the selected row, clear disturbance/repair evidence, and create a neutral
challenger. The new active row learns with slow forgetting. When the library would
exceed three rows, evict the row with the oldest last-fit timestamp. A model fits
when its error is within 10% of the best scored model or within 0.01 °C/interval;
the active model's last-fit clock is retained when archived.

Each configured temperature sensor creates two buttons and one switch, named after
its friendly sensor name and identified by its entity ID:

- **Accept new normal** immediately promotes its challenger and archives the active row.
- **Relearn room** resets only that room's active row and challenger to neutral, preserving its library.
- **Temporary change** holds its active model and stops all thermal training, scoring
  and automatic switching for that room. Fan rewards also exclude the room, invalidating
  the experiment if its priority share is significant. `temporary_hours` defaults to
  0 (manual); a positive value stores an absolute auto-off deadline. Toggle/expiry
  discard the partial room interval and old comparison blocks.

Global reset remains an explicit reset of all learning. Room controls preserve
house control state and other rooms. Options reload creates controls for added
sensors and removes registry entities and regime repairs for removed sensors.

A room continuously flagged disturbed for 12 hours raises a warning repair:
“<room> behaves differently than learned; if something changed, press Accept new
normal”. Settling, a switch, or a room reset clears it. `sensor.house_learning` adds
`active_model` (`main`, `saved #n`, or `promoted at <UTC timestamp>`), `saved_models`
with identity, last-fit UTC timestamp and coefficients, and `challenger_advantage`
(percent reduction in recent absolute prediction error) under each sensor. Temporary
status/deadline and disturbance start time are also exposed.

Store persists challenger/library rows, model identities, bounded comparison blocks,
last-fit clocks, identification stability, disturbance start and temporary deadlines.
Legacy Store files create neutral challengers and empty libraries; reconciliation
maps **all** rows' coefficients and covariance by input kind/entity ID. Changed input
columns clear comparison blocks. Runtime intervals still restart fresh.

The seeded 21-day simulations use independently cycling HVAC systems, variable
weather and sun, and measurement noise. The two-hour heater and four-hour nightly
unconfigured stove cause zero switches. Increased loss from day 3 promotes after
24 h 10 min, with heat-loss coefficient approximately 0.045002 (truth 0.045).
Portable AC from day 3 promotes after 24 h 10 min; removing it at the start of day
10 restores the original after 4 h, with its archived coefficients/covariance/counts
unchanged. Returning the AC one week later reuses its saved model after 4 h.
Tests also cover context/margin/coverage gates, eviction, old Store compatibility,
reconciliation, independent rooms, real HA buttons/switch, deadline restoration,
repair creation/clearing and sensor entity removal.


## 0.5.2 regression evidence and test changes

The shared-local-sensor replay starts living at 24.5 °C, downstairs at 19.4 °C and guest at
18.7 °C, weighted 1:2:2, targeting a house average of 22.5 °C. True per-interval effects are
A=[0.85,0.15,0.15] and B=[0.30,0.40,0.40]; both thermostats sense living. A has 200 effective
samples per room; B has 80 in living and only one in each cold room, with cold-room raw b=0.03.
Passive losses are chosen to admit a uniform 22.5 °C equilibrium with B at half duty, outdoors
7 °C. Binary thermostats run in one-minute physical steps; RLS learns the observed fractions.
These dynamics and weights are test fixtures, never controller configuration or assumptions.

On the first pass A's target is 1.0 °C below its own sensor and B's 0.5 °C above. At 12 hours,
weighted mean is **22.6295 °C** and weighted standard deviation **0.4381 °C**. With B unavailable,
the same house reaches mean **15.2539 °C**, weighted standard deviation **7.2731 °C**, peak
**7.3378 °C** at 12 hours. A alone cannot equalize or hold this house: its poor reach cannot
justify indefinitely heating living. At seven days standard deviation is **8.3400 °C**, peak
**8.3697 °C**, and the last day's range is **0.1316 °C**. Heat/cool grid searches separately
verify the remaining actuator's J minimum for λ=0,1,4; a two-actuator grid verifies joint optimality.
Tests also exercise outdoor loss, source, solar and drift contributions to the forecast.

The existing seeded two-day physical house retains its response matrix, losses, noise seed,
weather and cycling thermostat duty law. It now reports each thermostat's living-room sensor,
supplies current solar to prediction and feeds measured runtime back into effort translation.
The pre-change v0.5.1 final max−min spread was **0.152566 °C**; v0.5.2 is **0.151194 °C**
(learning disabled: **7.736648 °C**). Day-two mean range is **20.9450–21.0796 °C**, target 21 °C.
The original coefficient, convergence and exploration assertions remain, with an explicit
no-regression bound against the recorded v0.5.1 final spread.

Updated old assertions in `test_controller.py` replace independent PI/slew/house-relative target
expectations with own-sensor crossings, mean integration, effort dwell and device limits.
`test_learning.py` replaces independent-error/offset diagnostics and obsolete slew/clamp tests
with joint allocation and bounded exploration checks. `test_excitation.py` mirrors own-sensor
margins without the obsolete house clamp; its v0.4.1 comparison now freezes the historical PI
law as well as its prior blend, so the comparison actually remains v0.4.1. The current late-sensor
replay starts B at minute zero and ends at weighted standard deviation **0.8438 °C**, versus
**2.4516 °C** for that historical controller. `test_homeassistant.py` allows unchanged saturated
effort targets after warmer requests, changes the outside-change fixture to a value distinct from
the new off-effort target, checks joint/mean diagnostics on priority changes, and allows adjacent
rounding steps during the filtered transient replay. Sensor filtering/learning assertions remain.
All results are deterministic simulations, not measured performance in the house.

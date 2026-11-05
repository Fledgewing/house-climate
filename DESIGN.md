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
  effort hysteresis 0.05 and effort on/off dwell 5 min, reported minimum run 10 min,
  minimum off 5 min, maximum starts 3/hour, comfort band 1 °C total, switching cost 0.05
  and effort movement cost 0.005. Cycling limits have optional per-system overrides;
  `modulating` can override target-step-based capability inference. Legacy offset clamp, slew and PI
  gains remain readable for compatibility but no longer govern commands.

## Control loop (v0.5.4)

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
   current endpoint. Control and house mean/spread use these medians; learning uses
   the aligned time-weighted means described under v0.5.13 below. Reporting and
   staleness checks still apply to the raw member.
   Transient detection compares movement with both `max_rate` and four times a
   sensor's learned robust jitter scale. Ordinary jitter and quantized steps remain
   in the average. Real excursions are held out of the control median until return
   or `filter_minutes` expiry; their exact spans remain masked for learning.
   Short excursions do not break integrated learning windows. Missing sensor data
   and reporting failures still break the affected window. State retains bounded
   raw events, masks and jitter evidence for restart continuity; a restart always
   starts a fresh learning/runtime interval.
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

   `J(u) = Σ_i w_i (T_hat_i − T_hat_mean)² + λ (T_hat_mean − S)²
   + switch_penalty × Σ_j 1[on(u_j) ≠ committed_on_j]
   + effort_change_penalty × Σ_j |u_j − previous_u_j|`.

   Normalize the active positive room weights to sum to one. Use `average_weight`
   for λ (default 1, options 0.01–10), independently of the fan override. Lower λ
   prioritizes spread and deliberately permits a mean below S in heating. `learning.forecast` propagates each interval model
   `ΔT_i = a_i Δ_i + Σ_j (b_ij + g_ij Δ_i) u_j + Σ_k (e_ik + g_ik Δ_i) source_k + c_i solar + d_i`
   to the horizon (default 45 minutes), holding current outdoor, source states and solar
   constant. Here Δ_i is outdoor minus the room temperature at the start of the horizon.
   Hold Δ_i in the interaction terms while propagating passive losses every step; the forecast stays affine in effort.
   Missing context omits that term. All nonnegative b coefficients use the existing
   optimistic confidence blend, preserving their physical scale rather than normalizing
   each system's room effects. Without learning, use the same neutral b prior for every pair.
   No room mappings, individual room targets or overshoot ceilings enter the objective.

   Solve the small constrained problem using up to 200 cyclic coordinate minimizations,
   stopping when effort changes fall below 1e-9. Each coordinate evaluates zero, feasible
   endpoints, the previous effort and the quadratic stationary points on either side of
   the absolute-movement kink. The switch cost makes the objective nonconvex; this is a
   coordinate solution, not a claim of global mixed-discrete optimality. Heat efforts are
   0..1; cool efforts −1..0. Without control state, `allocate` retains the original pure
   convex thermal objective for independent forecasting and grid validation.

   A commanded or reported running unit inside `min_run_minutes` has magnitude at least 0.001;
   an idle unit inside `min_off_minutes` or at `max_starts_per_hour` has magnitude zero.
   Starts expire from the rolling window exactly one hour later. If every used,
   positive-weight filtered reading is inside setpoint ± `comfort_band/2`, retain the
   committed on/off state as another allocation bound. Otherwise leave state changes to J,
   even when the average itself is comfortable. This band never selects heat/cool mode.
   A system with a fine target step (<0.5 °C by default) can modulate; otherwise evaluate
   only full-on/off efforts. Per-system `modulating` overrides that heuristic.
   Unavailable systems have no variable. With no learned positive reach, the thermal objective cannot justify a new start;
   cycling/comfort holds and the state-change cost still apply to a running actuator.

   A single persisted mean-error integral biases S by at most ±1 °C. Its separate
   `mean_integral_gain` defaults to 0.001 per scheduled interval, multiplied by
   `average_weight / (1 + average_weight)`; immediate requests do not integrate.
   This smooth gain scaling weakens mean correction along with the mean objective
   (effective gain 0.0005 at λ=1, 0.000167 at λ=0.2). It does not alone eliminate
   long-term bias: the existing reachability gate below remains essential. Conditional integration stops increasing the bias when the predicted mean
   cannot follow it in the requested direction (0.05 °C² tolerance). Old per-system PI
   integrals remain loadable but are unused. This keeps upgrades from applying old PI
   gains or room errors to the joint objective.

   Translate positive effort magnitude q to `own_current_temperature + direction × run_margin × q`.
   Zero effort requests `own_current_temperature − direction × run_margin`. Thus low continuous
   output sits close to the local sensor. If the own sensor is missing, explicitly report
   using the house mean as a proxy. Retain effort hysteresis and legacy demand dwell as
   additional smoothing, with cycling and comfort bounds taking precedence. Round to the
   advertised target step (fallback 0.5 °C), then enforce the chosen demand side of the own
   reading so rounding/dither cannot reverse it. Device min/max still bound targets.
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
   In v0.5.7, explanation room names use active-priority weights: rank the restraining
   room by weight × directional deviation, and helped rooms by weight × reach × deviation.
   Never name rooms whose share of the available objective weights is below
   `minor_weight_share`. This changes text only, with no effect on allocation.
4. Run the fan/vane scheduler described below, adding at most one changed fan/vane command.
   Write only what changed: `climate.set_hvac_mode`, then `climate.set_temperature`. For a system with a
   configured required preset, re-assert that preset if it has left it.
5. Track each system's last successfully commanded mode and target. Report an outside change
   in `last_decision` only when the reported mode/target differs from that command and more
   than 2 minutes have passed since it, suppressing delayed device echoes. The next interval
   re-asserts (the House thermostat is the one control).
6. Fallbacks: if no member sensor is available, every system gets the house setpoint, subject
   to reported cycling holds. Without any local/house reference, retain an on-held target or
   use HVAC off for an off hold. If target limits prevent an off hold, also use HVAC off. If a system is unavailable, issue it no commands and keep controlling the others. Simulate its local thermostat as a fixed input at each horizon step (v0.5.5), with the normal objective weights unchanged.

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

`ΔT_i = a_i Δ_i + Σ_j (b_ij + g_ij Δ_i) u_j + Σ_k (e_ik + g_ik Δ_i) s_k + c_i solar_kW + d_i`,
where `Δ_i = T_out − T_i` at the interval start. System and source g columns are distinct.

Coefficients describe one configured control interval (5 minutes by default); elapsed time scales
regressors for delayed passes. `a_i` describes heat loss, `b_ij` the temperature response to each
system, `c_i` sun gain, and `d_i` background drift. Every system/room pair starts with the same
neutral `b = 0.03 °C/interval`. All other prior means are zero. Fixed coefficients have
diagonal prior covariance 100; interaction coefficients use `interaction_prior` (default 0.0001),
with zero cross-covariances. There are no room-specific assumptions.

The signed effort `u_j` is actual heating seconds minus cooling seconds, divided by interval
seconds. A state-change listener integrates every `hvac_action` transition, including transitions
during command service calls. Idle/off/fan/drying contribute zero; unavailable or unknown actions
invalidate that interval. Sampling only the action at a pass would miss short cycles.

The forgetting factor defaults to 0.998 per valid row sample. Effective activity counts follow
`n_ij ← 0.998 n_ij + 1[u_j ≠ 0]`. Confidence is `n/(n+N0)` with `N0 = 24` by default (about two
hours of active 5-minute samples to give learned and prior coefficients equal weight).
Control uses `max(0, confidence × learned_b + (1 − confidence) × system_prior + g × Δ_i + h)`.
Only the fixed part is confidence blended; the combined heating effect is clipped to zero.
Sources use signed `e + g × Δ_i + h` without blending or clipping. Joint prediction uses those physical coefficients directly; normalized per-system influences remain diagnostic. `system_prior` is the confidence-weighted mean raw learned b
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
Its `systems[system_entity][sensor_entity]` attributes expose raw fixed `b`, interaction `g`,
`h` (delayed effect), `blended_b` (blended fixed part plus g × current Δ plus h), clamped `effective_effect`, normalized
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

`fan_reward_lambda` is the optional fan-only λ override (options: 0–100).
Unset/None follows `average_weight` dynamically; explicit zero remains zero.
The optional options-flow field has no numeric default and clearing it restores following. This is the variance/mean-error reward
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
Outdoor band and runtime context changes split the observed moments by context within the
same hold, preserving its deadline and crediting each context with observed seconds.
Heating excitation/steady tests and fan/vane learning run independently, on the same unit
or different units: neither reserves a unit from the other. House target/effort commands
and test start, completion or early stop never dirty fan windows. Command echoes are
recognized through pending and last commands; real user setpoint/mode changes still invalidate. Events between ticks
latch dirtiness; recovery before a tick cannot make it train. Dirty windows retain their deadline
but contribute no sample. Disabled house/fan control invalidates experiments and sends no fan/vane
commands. Missing/stale/not-reporting minor members are excluded from fan rewards;
transients are masked by duration in thermal learning while fan rewards use filtered values. Commands use `climate.set_fan_mode` / `climate.set_swing_mode` only when different.
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

`extra_heat_sources` is an optional list of `{entity_id, kind, threshold, response}` records in
entry data/options. The ordinary membership page selects any binary-like or numeric
entity; each source page chooses `binary` (literal HA on/off states) or `power`
(finite W, kW or MW, strictly above `threshold` W, default 10). No heat-source device
names or room mappings appear in implementation. Sources are never commanded.
As of v0.5.7, `response` defaults to `on_off`, preserving existing entries. Power
sources may instead select `proportional`: effort is W/1000 (after W/kW/MW conversion),
zero at or below the threshold standby floor. Binary sources remain on/off.

RLS columns are `[outdoor−room, HVAC efforts..., source fractions..., solar, 1]`.
Source coefficients have zero prior means, covariance diagonal 100 and zero initial
cross-covariances. Source effective activity counts use the same forgetting and
`n/(n+N0)` confidence as HVAC inputs. Sources use a separate `EffortTracker`, accepting numeric effort as well as the
HVAC action strings. On/off translates to 1/0; proportional sources integrate kW.
Both listen to every state transition even during command locks. Both
trackers finish at the same scheduled passes; immediate control requests leave their
integrals intact. Unknown observations make the interval unavailable. Observations
record the integrated fractions or time-weighted mean kW. Forecasts use current effort. Startup and pause/resume reset the runtime baseline,
so no unobserved interval is invented.

Reconciliation maps covariance and coefficients by `(input kind, entity ID)`, retaining
all surviving cross-covariances, fit errors, sample counts and effective activity counts.
New columns alone start neutral; source removal shrinks rows without resetting HVAC,
solar or drift. Persist source response modes alongside the model. On reload, a
changed response resets only that source's coefficient, activity count and covariance
column to neutral priors, including challenger and saved regime rows. Other columns
remain intact. Old Store models default to `on_off`.
`sensor.house_learning.sources[source][room]` exposes `e`, confidence percent and
`effective_samples`; `sources[source]` also exposes `response` and current `effort`
(0/1 or kW, null for unknown). Coefficient units are °C per full-on interval or °C per
kW per interval. Overall confidence remains the HVAC-pair mean used previously.

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


## Cycling protection (v0.5.3) and regression evidence

`observe_action` maintains persisted per-system `last_start`, `last_stop` and `starts` (the
rolling hour only), alongside the existing `last_run`. Only actual heating/cooling actions
count as running; repeated reports do not move start/stop clocks. Initial known observations
start conservative clocks when no earlier evidence exists. Recorder replays retained actions,
including attribute-only changes, before the first control pass; Store and recorder clocks
merge by latest evidence and start timestamps are deduplicated. Restarts retain the last
action identity but reset its runtime-accounting endpoint to now: downtime earns no observed
runtime and a still-running compressor does not acquire another start.

Minimum run/off and start-cap constraints enter allocation and run again after drive feedback,
dither, excitation and rounding. New excitation cannot start an off-held unit or break an
all-rooms-comfortable hold; an active excitation cannot override the final bounds. Off/veto
and unavailable/observe-only behavior retain their existing precedence. Physical compressor
self-cycles are outside demand control; their reported transitions still update protection.
`climate.house.cycling[entity]` exposes `starts_last_hour`, `run_minutes` and `off_minutes`.
Decision notes explain held on/off for cycling protection. A fixed House direction never
commands its opposite, including the no-room fallback; `heat_cool` alone selects a reversal.

The [EA Technology/DECC cycling report](https://assets.publishing.service.gov.uk/media/5a78e0d9e5274a2acd18a7c6/7389-effects-cycling-heat-pump-performance.pdf)
found approximately eight-minute ASHP runs near steady-state COP and an optimum around
10–15 minutes for the tested GSHP. The generic ten-minute run default is conservative.
[Trane](https://www.trane.com/residential/en/resources/glossary/hvac-short-cycling/) documents
five-minute compressor off protection; [Mysa](https://getmysa.gorgias.help/en-US/understanding-heating-or-cooling-delays-3619484)
documents a three-cycle/hour heat-pump setting. These support tunable defaults rather than
an assertion that all manufacturers specify the same limits.

The new four-hour noisy replay (`tests/test_cycling.py`) uses one-minute control, two equally
weighted rooms initially 20/21.85 °C, a 21 °C setpoint, and alternating ±0.15 °C common sensor
noise plus seeded ±0.005 °C jitter. Per-minute full heat effects are 0.013/0.011 °C with losses
half those values; rooms remain outside the comfort band so balancing decisions stay eligible.
The unprotected comparison disables cycling limits and movement/switch costs but uses the
same controller, plant and readings. The replay serializes/restores state midway.

| Four-hour metric | Unconstrained | Protected |
|---|---:|---:|
| State changes / starts | 240 / 120 | 18 / 9 |
| Minimum completed run / off | 1 / 1 min | 11 / 13 min |
| Maximum rolling-hour starts | 30 | 3 |
| Mean absolute average error | 0.072000 °C | 0.074800 °C |
| Mean max−min spread | 1.849500 °C | 1.849967 °C |

The average-error cost is 0.0028 °C and spread cost 0.000467 °C. The initial left-censored off
interval is excluded from duration minima. Tests separately exercise exact 10-/5-minute and
one-hour boundaries, delayed reports versus commands, room-outlier starts with the average in
band, all-room band holds for both reported states and both directions, modulation, objective
penalties, device limits and missing-reference holds. HA tests seed an active run, a recent
stop and three recent starts from recorder, then save/reload Store and verify constraints.
Fourteen fixed-mode guard cases cover allocation, excitation, both cycling holds, fallback,
rounding and an opposite reported action; a separate test permits reversal in `heat_cool`.

Existing binary shared-sensor replay: 12-hour mean 22.6444 °C, weighted standard deviation
0.4362 °C (v0.5.2: 22.6295/0.4381). Late-sensor B starts at minute 5 after the observed off hold;
final weighted standard deviation is 0.8361 °C versus frozen v0.4.1's 2.4516 °C, with day-two
mean standard deviation 0.8550 °C. The fractional-duty learning fixture still identifies every
b within 0.003 °C/interval and holds day-two mean within ±0.5 °C (20.5269–21.2575 °C).
Its final max−min spread rises to 0.6888 °C from 0.1512 °C as the new band intentionally holds
comfortable units. Its old exact-spread assertion becomes a 0.75 °C bound, and the old
all-exploration-ended assertion becomes >80% mean confidence (actual 84.03%); steadier output
can leave individual room/system pairs exploring longer. Other historical fixture assertions
remain. HA fixture updates choose an outside target distinct from the new protected-off target,
allow new system options, establish old off evidence before testing excitation, and expect
persisted action identities with fresh runtime endpoints on reload. These are deterministic
simulations, not measured performance.


## Unavailable systems (v0.5.4)

Persist the last known reported HVAC action, signed effort and display name separately
from current runtime observations. Heating/cooling contributes full signed effort for
cycling units; modulating units use the last reported target relative to their own
reading, divided by run margin and bounded to 0..1 in the reported action direction.
Idle/off/fan/drying contributes zero. Missing target/own readings fall back to full
reported running effort; with no known action, assume off. Unknown reports do not
replace retained evidence. During an outage, fold each retained input through its
system's forecast coefficient into the passive forecast, outside the decision variables.
The retained assumption survives Store restore, and `last_decision` names the system
and its assumed action. Current unknown runtime still skips learning intervals.


## Unavailable local thermostats (v0.5.5)

Unavailable units retain their own last reported temperature, target, mode and on/off latch.
Advance that temperature once per elapsed interval with its learned row and the joint signed
system efforts, outdoor loss, solar, extra sources and drift. Fractional intervals advance
proportionally; the saved timestamp prevents immediate ticks or restarts from advancing twice.
Use a real, fresh reporting own-sensor reading whenever available. The coordinator prefers
an explicit per-system `own_sensor`, then a unique configured sensor on the same HA device;
otherwise it learns a separate row keyed by the climate entity's `current_temperature`.
These additional rows never join the house comfort weights. Unknown runtime still excludes
learning; predicted temperatures and actions are never training observations.

For heat, stop at or above the retained target and restart strictly below target minus
`thermostat_hysteresis` (default 0.5 °C); reverse both inequalities for cool. Inside the gap,
retain the latch. When observed idle-to-running transitions at an unchanged target exist,
use the mean target-relative starting gap from the last 32 transitions instead of the default.
Retained samples or recorder seed this evidence when no retained thermostat exists. Save thermostat state,
timestamps and hysteresis evidence in `ControlState` through the existing Store path.

For each allocator candidate, forecast copies of all unavailable thermostats simultaneously,
using that candidate's available efforts and the unavailable units' predicted signed on/off
inputs. An unavailable unit can stop and restart within the horizon, but is never an allocator
decision variable and receives no commands. Since the resulting objective is piecewise,
evaluate binary coordinate candidates directly; for modulating units sample 41 bounded
candidates and refine the best with 18 halving steps, retaining the existing movement and
switch penalties and cycling bounds. This is a bounded coordinate search, not a global
optimality guarantee. Units without a retained target/own reading keep the v0.5.4 reported-input
fallback until sufficient real evidence arrives. `last_decision` names each unavailable unit,
its predicted own reading and retained target, and explains whether it is heating, cooling or
off. Forecast copies never advance persisted time or the reported runtime evidence.

Tests cover heat/cool target crossings in both the horizon and elapsed outage state,
fresh real own readings, JSON/Store restart continuity, hysteresis latching and learning,
and own-row contributions from joint efforts, outdoor temperature, solar and heat sources.


## Average/balance separation (0.5.6)

Config-entry version 4 adds missing `average_weight=1.0` to the effective options,
preserving an existing value and all explicit fan overrides. Legacy fan weights no
longer affect allocation. New entries leave the fan override unset; coordinator
construction, Store restoration and learning reset all resolve that override in the
same way. Existing fan statistics are re-scored using the resolved weight.

The 12-hour weak-basement regression and its assumptions/results are recorded in
README's “Average versus balance” section. It runs the controller with its default
integral enabled at λ=1/0.5/0.2/0.1 and compares λ=0.2 with integral disabled.
The mean-reachability gate blocks integration throughout this fixture, so lower λ
cannot be wound back toward the setpoint. Separate tests verify gain scaling when
the target is reachable. The two-day thermal-learning benchmark explicitly retains
its original effective mean gain (configured 0.002 × 1/2), isolating model-learning
accuracy from this intentional change in integral dynamics. This objective provides
no hard 0.5 °C gap bound: forecasts, passive losses and actuator reach determine
what spread can be achieved.


## Temperature-dependent system and source effects (v0.5.8)

Rows use `[Δ_i, systems..., sources..., system interactions..., source interactions..., solar, 1]`.
Each interaction is the same signed system effort or source value multiplied by the room's
start-of-interval outdoor–indoor difference. Duration multiplies the whole regressor vector.
Both active and challenger fits, saved-model scoring, and ordinary forgetting use these columns
exactly like existing columns. No device-specific output curve or fan assumption is supplied.

`interaction_prior` is a positive covariance tunable in Configure, default **0.0001**, versus
**100** for fixed effects: a million-fold tighter covariance for the slope. A 10 °C difference
therefore contributes prior predictive variance 0.01 from a slope, versus 100 from its fixed
part. This gives fixed effects the initial freedom to explain runtime, keeping g near zero
when effort × Δ is nearly collinear with effort. Repeated observations with different Δ identify
the slope; forgetting gradually relaxes its prior as it does every other coefficient. This is
regularization, not an identifiability guarantee: constant outdoor temperature with changing room
temperature can still provide Δ variety, and synchronized inputs remain hard to separate.
Changing the option affects new/migrated/reset columns, retaining fitted covariances.

Forecasts freeze interaction Δ at the start of the requested horizon so effort allocation remains
affine. Passive losses still propagate. Outage thermostat simulation reevaluates Δ at each
simulated interval's starting temperature. Allocation, normalized influences, explanation reach,
and eligibility to start/continue inactivity excitation all use the effective heating effect.
Unknown outdoor or room context omits the interaction, preserving the fixed-effect fallback.
Source effects remain signed: a fan can cool with colder outdoor air and warm with hotter air.
`sensor.house_learning.sources[source][room]` exposes fixed `e`, slope `g`, and signed
`effective_effect`; systems expose `b`, `g` and clamped blended `effective_effect`. Diagnostics use
the current outdoor sensor/weather fallback and current filtered room readings. Slope units are
°C per interval per input unit per °C difference; fixed/effective units match the input's existing
on/off, signed runtime, or kW units. Missing context evaluates the interaction as zero.

Loading v0.5.7 Store rows inserts zero-mean interaction columns before solar/drift, using the
configured prior covariance and zero cross-covariances. Every old coefficient, covariance entry,
activity count, sample count, fit error and regime identity/evidence is retained. This applies
to main, challenger and saved regime rows; zero slopes reproduce v0.5.7 predictions exactly.
Reconciliation maps fixed and interaction columns by input kind/entity ID, including full cross
covariance. The v0.5.7 source response-change path removes/reintroduces both columns so changed
units reset that source alone. No learning reset accompanies the upgrade.

Deterministic five-minute simulations (`tests/test_interactions.py`, seeds 508 and 580):

- Heat pump: twelve training days with daily outdoor swing 0–15 °C, independent varying duty,
  heat loss and 0.0005 °C noise; then freeze both fits for three held-out days. Truth is
  `b=0.4`, `g=0.012`: colder outdoor temperatures reduce output. Learned **b=0.39882142**,
  **g=0.01189690**. One-interval prediction RMSE is **0.00053412 °C** versus **0.01787796 °C**
  for the v0.5.7 fixed-only RLS on identical observations (about **33.5×** lower).
- Whole-house fan: twelve days with the same outdoor swing, independent HVAC duty and fan
  on/off, truth `e=0`, `g=0.02`. Learned **e=−0.00093106**, **g=0.01992620**. Tests check both
  cooling and warming signs.
- Constant Δ commissioning: outdoor 7.5 °C, room starts at 20 °C each observation, varying duty
  and the same noise. Learned **g=−0.0000027515**; frozen three-day RMSE is **0.00050911 °C**
  for both fits and maximum allocated effort difference is below **1e−9**.

These are simulated results, not measured house performance. Existing physical/control tests
retain their thresholds; their explicit coefficient/covariance fixtures gain zero interaction
columns. Migration tests check every retained covariance entry in all three row roles, including
a saved regime. Additional tests cover duration/signed cooling regressors, reordered inputs,
source response resets, diagnostics, affine forecasts, outage thermostat predictions and the
nonnegative combined system heat clamp.


## Delayed effects and informative experiments (v0.5.9)

This section supersedes the historical v0.4.2 excitation policy above. The interval model is

`ΔT_i = a_i Δ_i + Σ_j [(b_ij + g_ij Δ_i) u_j + h_ij L_j]
 + Σ_k [(e_ik + g_ik Δ_i) s_k + h_ik L_k] + c_i solar + d_i`.

`L` is the time-weighted mean signed effort in the `lag_minutes` preceding the interval start,
excluding the current interval. The default window is 60 minutes, adjustable in options
(5–1440 minutes). All columns, including L, scale with elapsed/configured interval duration.
Binary and proportional source efforts use exactly the same history and regressors.
Keep bounded observed action/effort segments, timestamps and missing markers in Store, with one
preceding interval for boundary coverage. Runtime trackers retain transitions within each pass,
so partial-window overlap uses actual time weights, including proportional sources. Direct
model callers that only supply interval averages retain those averages as held segments. Unknown actions and restart gaps never acquire
invented effort. Incomplete lag coverage gives a zero feature, with no coefficient or variance
update credit for that lag column. A zero-gain covariance update adjusts cross terms to keep
the joint posterior positive while retaining the missing coefficient and its variance. Other observed features can still train. Paused thermal
learning and withheld room samples retain effort observations for subsequent predictions.
Runtime confidence counts keep their existing nonzero-current-effort meaning.

Each input gets a zero-mean h with diagonal `lag_prior` covariance, default 0.01, and initially
zero cross covariance. This is 10,000 times tighter than the broad fixed-effect prior (100),
while permitting a 0.1 °C/interval standard deviation (1.2 °C/h at five minutes).
Unlike g, h is not multiplied by an outdoor difference typically of order 10 °C: its default
standard deviation is therefore ten times g's 0.01, giving comparable prior temperature scales.
The prior discourages attributing ordinary drift to delayed heat without suppressing a remote
room response of 0.025 °C/interval (0.3 °C/h). Both priors are tunable.

Column order is `[delta, inputs, inputs*delta, lagged_inputs, solar, 1]`. Loading a v0.5.8
row inserts h priors before solar/drift and preserves every old coefficient, cross covariance,
count, error and sample. Apply the same mapping to challengers and saved regimes; v0.5.7
stores gain both missing groups in one migration. Reconciliation matches all three input groups
by kind/entity ID. A source response change removes/reintroduces all three columns and its
history, including challenger and library rows, with neutral priors and no learning reset.

Steady effects include h everywhere: blended/effective diagnostics, normalized influences,
excitation covariance and allocator/outage forecasts. Only b is confidence blended, and systems'
combined steady heating effect is still clamped nonnegative; sources remain signed. Forecasts
must not apply the full steady h immediately. At horizon step k (time t from horizon start),
`L_hat(t) = integral[past effort from now+t-window to now]/window
 + min(t/window, 1)*candidate_effort` for t below the window; afterward it is candidate effort.
Hold the outdoor interaction delta at the horizon start, propagate passive retention each step,
and add known lag as an intercept and future lag as an effort coefficient. This remains affine.
Outage rollouts append the local thermostat's predicted joint efforts to private history each
step, allowing its delayed response to continue after it stops; they never train real history.

The information score for system j is `Σ_i active_weight_i × std(b_ij + g_ij Δ_i + h_ij)`.
Use the full posterior covariance, including cross terms, and omit rooms below
`minor_weight_share`. As of v0.5.10, choose the highest score among eligible systems above
`(1 - explore_threshold) × PRIOR_B`; the default threshold is 0.006 °C/interval.
Exclude systems already reporting heating/cooling or receiving nonzero allocated effort:
their continuing normal runs supply steady-test evidence. Check every candidate independently;
unavailability, missing local temperature, cooldown, cycling holds, unsafe forecasts or device
target limits on a higher-score unit never block an eligible lower-score unit.
Increasing the existing exploration setting tolerates less remaining uncertainty;
zero disables exploration. Whole-system runtime confidence does not gate tests.
Rank named rooms by their contribution to this score and name the top two in `last_decision`.
`sensor.house_learning.experiments` exposes each score, last qualifying steady test time,
next eligible time, and current `skip_reason` (null for a selected/eligible system). Reasons
include "running steadily", "cycling hold", "forecast above limit", "not uncertain enough",
and "house above setpoint" ("house below setpoint" in cool). A qualifying allocator or experimental run is continuously observed
matching HVAC action with effort stable within 0.05 for at least `excite_minutes` (default 60).
Short runs never reset the test clock. With no prior test, the initial observation starts the
eligibility wait. Store retains qualifying times; restarts clear open run endpoints.

Tests require the house mean at or below setpoint in heat (at or above in cool), including
inside the comfort band. Forecast every interval of the proposed run against the unmodified
baseline allocation, holding other systems' allocated effort for the run. Every non-minor room
may reach setpoint + `test_overshoot` in heat (mirror below setpoint in cool).
In v0.5.10, `test_overshoot` defaults to **1.5 °C**, tunable in options (0–5 °C).
A room starting above that limit vetoes a start only if any forecast step would raise it
further by more than **0.3 °C** over the run (mirror for cool). For these rooms, retain
the starting temperature plus 0.3 °C as the ceiling throughout the test so the allowance
cannot grow on successive passes. Rooms initially within the limit must remain within it.
Use these same per-room ceilings for the start check, current readings and forecasts on
every pass over the remaining run; skip/abort on breach, missing house data, mode change
or unavailable unit. The house-average condition is unchanged.
Cycling constraints retain precedence. One global test runs at full steady effort, with no
dither on any system and no mode selection caused by a test. Targets track the own sensor as
needed to retain full effort. Complete after the configured observed steady duration; interrupted
runtime does not accumulate across stops. A two-duration wall timeout bounds failed attempts.
Aborts get a retry delay but do not become successful steady tests.

The seeded delayed-house regression uses independent on/off efforts, a pure eight-interval
(40-minute) transport delay, and true steady response 0.300 °C/h. After 12,000 five-minute
samples, v0.5.8 same-interval RLS learns 0.01531 °C/h; v0.5.9 learns 0.29402 °C/h. Its
120-minute forecast makes the allocator use A for D. Separate controller tests create repeated
five-minute runs that never qualify, then verify a 60-minute constant experiment, increased D
confidence, room safety in heat/cool, early forecast aborts, cycling, mode guards, source history,
missing/restart coverage, horizon affinity and lossless main/challenger/library migration.


### v0.5.10 validation

Heat/cool live-shaped tests keep a higher-uncertainty M running under reported action or
normal allocation while idle B starts with its room 1.2 °C beyond setpoint. At 2.0 °C beyond
setpoint, a further 0.4 °C forecast vetoes B, while a 0.2 °C forecast permits it. Tests
verify early stops and that cooldown, cycling, forecast and device-limit failures on a
higher-score candidate do not starve another system. Options persist `test_overshoot`;
the learning sensor exposes per-system skip reasons.

The existing seeded two-day physical replay now includes the revised experiments with
the default 1.5 °C allowance. It learns all original response coefficients within the
unchanged 0.003 °C tolerance; day-two house mean spans 20.0777–21.2457 °C and final
room spread is 1.0074 °C (learning disabled: 18.3733 °C). Its comfort assertions now
allow 1.5 °C for these deliberate tests; coefficient, early convergence, exploration and
confidence assertions remain unchanged. These are deterministic simulations.


Each configured unit has `switch.house_<system object id>_control`, default on,
persisted in Store and translated. Turning it off sends HVAC off once if currently
in heat/cool, then leaves all hand changes alone. It receives no targets, presets,
fan/vane commands or mode re-assertions. A clean fan window on that unit credits
its observed portion and ends; other units retain their holds.
`active_systems()` is the coordinator's common selection boundary: allocation,
excitation/steady tests, uncertainty ranking, cycling holds, outage thermostat
prediction and the global fan scheduler see only enabled units. Observation paths
explicitly request all configured systems so room/device learning continues with
the disabled unit's effort fixed at zero. Last-decision text lists disabled units
only as under hand control, and states when every unit is under hand control.
Re-enabling restores normal house-mode control on the next pass, with observed
cycling clocks retained. The house thermostat and remaining units continue normally.

## v0.5.11: command cycling and integrated identification

The v0.5.10 allocator used only `actions`, `last_start` and `last_stop` from
`observe_action` for cycling constraints. `control` could command on without
updating any of those clocks. At the next pass an idle/old report therefore permitted
zero effort before any minimum run had begun. A run that began and ended between
reports could also escape the start quota. The state penalty compared candidate
on/off to the reported action, rather than the last command. Its reference could
move with a delayed report instead of providing hysteresis around the command.
The every-room comfort hold did not help this night: downstairs remained far
outside the band even when the average was comfortable. Endpoint efforts 0/1 are
not created by drive bias; dither cannot promote zero to positive effort, and the
final target-side clamp already prevents rounding from reversing demand.

`command_demand` now reserves each transition at decision time, conservatively
before sending services. Separate command start/stop clocks survive persistence.
The later action start confirms the reservation rather than double counting it;
its later timestamp extends minimum run protection. Reported stops extend off
protection, and autonomous reported cycles still count. Both command-time and
report-time rolling quotas must permit a start. A confirmed thermostat stop after
acknowledgement enforces the off delay even if the previous command remained on.
Failed sends retain the reservation: losing a slot temporarily is safer than
issuing another start immediately. Hand-control handoff records its off command.
Explicit House off remains an immediate override.

The hysteresis rule is now stable across delayed reports: keep the committed
command unless the forecast objective improves by more than `switch_penalty`
(default 0.05 °C²) plus the L1 movement cost. If every weighted room is inside
setpoint ± comfort_band/2, keep that command regardless of forecast preference.
Run/off/quota constraints override both rules and are re-applied after excitation,
feedback, dither and target rounding. No larger arbitrary switch penalty is needed.

### Learning changes

The coordinator's existing EffortTracker already integrates all reported action
transitions, including service-call events; the thermal update receives elapsed
heating/cooling fractions, not `last_decision` effort. Its hand-control exclusion
remains in place; after the other unit was switched off, its measured fraction
and excluded fraction are both zero. This does not explain the Bryant-only segment.
Device own-temperature learning rows are separate RLS rows: they do not directly
enter another room's regression. They *did* enter the shared system prior used by
the old confidence blend, so a thermostat sensor's response could distort every
low-confidence room's forecast.

Production Learning now uses `window_minutes=48` (rounded up to whole elapsed
passes). The pure Learning API retains zero as its per-interval compatibility mode;
short wiring tests select that mode explicitly. A bounded per-room queue sums each
interval's temperature difference and all its regressors: outdoor difference,
actual runtime, temperature interactions, historical lag runtime, sources, solar
and constant. Dividing by total elapsed duration keeps coefficients in their
existing per-configured-interval units. The rolling fit updates each scheduled
pass once a full clean window exists. Gaps and invalid endpoints discard only the
affected window; missing global context clears all windows. Pause/restart discard
incomplete windows rather than joining unobserved time.

Control keeps the robust time-weighted median. Learning instead uses validated
raw endpoints, because applying a median to temperature alone delays its response
relative to the measured effort. Confirmed/pending sensor transients still exclude
rows and break windows; they are not reclassified as HVAC heat. Background drift's
zero prior covariance is 0.0001 in integrated mode, rather than 100. This regularizes
the poorly identified constant/outdoor tradeoff without removing the constant,
interaction, lag, source or regime terms. Integrated source-effect covariance is
0.01 rather than 100: a nearly constant source must not recreate an unconstrained
background intercept. Source means remain zero initially and can learn either sign. Existing measured rows use their own
regularized coefficients, avoiding the shared thermostat-sensor prior.

Each overlapping observation earns elapsed/window fractional activity credit;
forgetting uses the same fractional exponent. Runtime confidence is additionally
capped at `effect² / (effect² + steady_variance)` for b+h. It therefore remains low
when covariance cannot separate heating from lag/background, even after many active
passes. Upgrade retains physical coefficient means, resets background drift to
zero, and reopens covariance/activity counts from the old per-pass identification
scheme, including challenger/library rows. No completed learning windows persist.

### Night 11-04 evidence and independent check

Fixture: `tests/data/night-1104.txt`, including its original header. There are 180
four-minute aggregate rows, 21:30–09:26 PT. It is not raw state-change history, a live
Store snapshot, or a record of configured room weights. Consequently it cannot
reconstruct the exact live event ordering, rapid transients, or saved covariance.
The constructed small-effect prior below has b=0.004 °C per four-minute interval,
100 activity samples, and covariance 0.001; it exercises recovery from confident
small effects without claiming to reproduce the unavailable live Store.

Independent OLS uses overlapping 48-minute windows, endpoint warming in °C/hour,
and mean time-weighted Bryant fraction, Mitsubishi fraction, and outdoor-minus-room
temperature. It uses no RLS, lag coefficients, confidence blend or fitted-model
predictions. Including Mitsubishi permits the entire night to contribute while
accounting for its operation before ~23:33. Constant background is zero in this
independent check; fitting it freely on the Bryant-only portion is ill-conditioned
(condition numbers 153–364), allowing physically implausible negative losses in
Annabelle/Wesley and background cooling near 1 °C/hour. Zero is an explicit
regularization assumption, not proof that other heat sources emit no heat.

All effects below are steady °C/hour, evaluated at outdoor 11 °C and the final
recorded room temperature for the replay models. “Before” runs the v0.5.10 algorithm
with its control median and normal rejection. “After” uses validated raw endpoints,
48-minute windows and the new priors/confidence treatment. The full replay also supplies the
Soleia, server and papa power columns as proportional heat sources
(10 W units converted to kW), and the two bedjet flags as binary source channels.
Solar is zero in this replay. If Soleia instead represents a solar proxy, the
alternate context replay gives 1.107/0.515/0.502/1.208/1.424/1.909 °C/hour, also
within 35% of the simple independent fit.
Papa power and mummy activity are zero throughout, so they contribute no heat.
These context assignments are explicit replay assumptions; the export has no
entity configuration. The independent check intentionally remains a simple
HVAC/outdoor fit, so extra-source attribution can explain modest differences.
Every measured room is
within 35% of the independent whole-night estimate, from either initialization.

| Room | Independent OLS | Before fresh | Before small prior | After fresh | After small prior |
|---|---:|---:|---:|---:|---:|
| Living | 0.990 | 0.484 | 0.377 | 1.262 | 1.262 |
| Downstairs | 0.485 | 0.023 | 0.061 | 0.502 | 0.502 |
| Guest | 0.593 | 0.034 | 0.113 | 0.475 | 0.475 |
| Parent | 1.344 | 0.493 | 0.312 | 1.293 | 1.293 |
| Annabelle | 1.596 | 0.248 | 0.338 | 1.485 | 1.484 |
| Wesley/Gabriel | 1.774 | 0.199 | 0.523 | 1.733 | 1.732 |

The own thermostat row is diagnostic, not a weighted member: OLS 0.398, old fresh
-1.301, new 0.068 °C/hour. Its strong local cycling temperature is unsuitable as a
house warming-rate proxy. New room confidences are respectively 3.5%, 0.6%, 0.5%,
3.7%, 4.7%, 6.5%, versus about 74–75% old fresh and up to 85% with old runtime evidence.
Negative old values above precede the forecast's nonnegative heat clamp.

As a sensitivity check, Bryant-only fits after 23:38, excluding Mitsubishi, give
32/48/60-minute-window effects: living 0.658/0.684/0.668, downstairs
0.443/0.452/0.468, guest 0.597/0.609/0.624, parent 1.437/1.500/1.478,
Annabelle 1.553/1.715/1.729, Wesley/Gabriel 1.730/1.743/1.704. Living is sensitive
to the earlier operating regime: dropping it changes the fitted outdoor-loss
coefficient from 0.0453 to 0.028 °C/hour/°C and lowers Bryant reach by 0.306 °C/hour.
The whole-night multivariate estimate is the acceptance reference; this sensitivity
is a limitation of the export's identification, not evidence of 1% physical accuracy.

Candidate-cause ablations below isolate HVAC/outdoor identification with solar
and extra sources omitted (fresh prior, same fixture). That reduced-context old
replay gives living/down/guest/parent/Annabelle/Wesley =
0.654/0.071/0.135/0.637/0.437/0.269 °C/hour:

- Quantization is real: downstairs and guest have **97.8% zero four-minute
  differences**, living 80.4%. Per-pass differences lose the accumulated response
  and associate isolated steps with whichever duty fraction happens at that pass.
- Response alignment is real: removing only the control median changes old effects
  to living/down/guest/parent/Annabelle/Wesley =
  1.701/0.715/0.761/1.428/2.498/2.285. Raw per-pass fitting alone is still noisy.
- Rejecting disturbances is not the fresh replay's primary cause: zero rows are
  fully disturbed. Disabling innovation clipping/rejection changes effects to
  0.752/0.170/0.394/0.448/0.644/0.786, still wrong. The old small prior produces
  68 disturbed row-passes with broad covariance. In the full-context replay, the
  fresh old model rejects 148 row-passes (37 each for living/parent/Annabelle/
  Wesley), whereas the tightly held small prior rejects none. New integrated
  fitting rejects zero in both initializations. Four-minute means
  trigger no rate excursions at 0.5 °C/min, so this file cannot reproduce or rule
  out the live “sensor transient ignored” messages from faster raw reports.
- Lag cancellation is not the primary cause: removing lag covariance gives
  0.604/0.059/0.130/0.620/0.406/0.126. Removing forgetting gives
  0.686/0.059/0.119/0.620/0.408/0.216. Neither repairs the estimates.
- Constant/background regularization alone gives
  0.512/0.061/0.121/0.589/0.424/0.250, also insufficient. Integrated windows while
  retaining broad background covariance give living 1.912 and the other rooms
  0.420/0.552/1.366/1.497/1.656. Both alignment/integration and regularization matter.
- The own sensor does not dominate room regression, but the old shared-prior blend
  can propagate its erroneous reach into other rooms. Integrated measured rows
  no longer use that cross-room blend.

### Regression scope

The night controller replay uses equal weights for the six exported room members,
setpoint 22, one controlled binary HVAC, the learned model, and one-pass-late reports.
It feeds recorded room/outdoor/own-sensor inputs, with a simple counterfactual room
correction from the independent rates times the difference between new and recorded
heating fractions, decaying by 0.995 per pass. This is a deterministic sensitivity
plant, not recovered physical ground truth. Exploration is disabled to isolate
allocation; separate excitation tests retain its safety constraints.

It produces 11 starts over the replay, completed runs of 16–20 minutes, off periods
at least 12 minutes, and no more than 3 starts in any rolling hour. Mean absolute
house error is 0.394 °C versus recorded 0.272; mean spread 2.786 versus 2.934 °C.
The explicit materiality limits are +0.2 °C for each metric. Tests also cover an idle
report after a pending command, acknowledgement without a duplicate start, and the
later report-time quota at the hour boundary. A separate stochastic integrated
simulation checks delayed HVAC response and a measured heat source. Existing delayed,
interaction, source and regime simulations remain in the full regression suite.

The rate filter also now compares cumulative movement against a minute-old anchor,
with a minimum one-minute movement allowance. Previously a 0.1 °C change arriving
one second after a report opened a pending excursion for up to 15 minutes; harmless
quantized/report bursts could repeatedly withhold learning. The new rule admits
that small step but still quarantines cumulative movement beyond max_rate over a
minute, including many individually small fast steps. Large excursion and sustained
step expiry behavior remain covered. This addresses a structural cause of excessive
transient rejection without claiming to reconstruct sub-minute events from means.


## v0.5.12: tunable learning window

`DEFAULTS["learning_window_minutes"]` is 60 minutes, used by the coordinator
for both new and restored models. The configuration and options tunables step
exposes 15–240 minutes in one-minute increments. The pure Learning API retains
its zero-window compatibility mode.

The v0.5.11 48-minute integrated models already use the same coefficient units
and integrated evidence version as 60-minute models. `from_dict` and `reconcile`
therefore preserve active, challenger and saved model coefficients, full
covariances and activity evidence. Only the earlier per-pass evidence migration
reopens uncertainty. In-progress integrated windows are not serialized and refill
after reload; changing the window does not reset learning. A coordinator regression
loads a trained 48-minute model at 60, checks all rows and regimes for equality,
and verifies no update occurs until a full new window has accumulated. It covers
both an explicit 60-minute setting and an older configuration without the setting.

The historical v0.5.11 results above remain at 48 minutes. The same night acceptance
assertions now run at the default 60 minutes against the unchanged independent
48-minute OLS reference:

| Room | Fresh (°C/hour) | Small prior (°C/hour) | Fresh confidence |
|---|---:|---:|---:|
| Living | 1.271318 | 1.270509 | 3.100% |
| Downstairs | 0.541438 | 0.540679 | 0.617% |
| Guest | 0.528140 | 0.527364 | 0.571% |
| Parent | 1.289562 | 1.288791 | 3.224% |
| Annabelle | 1.531161 | 1.530360 | 4.381% |
| Wesley/Gabriel | 1.708868 | 1.708104 | 5.639% |

Every room remains within 35% of independent OLS, with confidence below 20%
throughout any materially inaccurate estimates. The delayed-report controller
replay has 12 starts, minimum completed run 16 minutes, minimum off 12 minutes
(excluding the initial off period), and at most 3 starts per rolling hour. Mean
absolute house error is 0.372406 °C versus recorded 0.271911 °C; mean room spread
is 2.793989 °C versus recorded 2.934266 °C. The unchanged acceptance limits allow
at most +0.2 °C for each comfort metric. The counterfactual plant and fixture
limitations described above still apply.


## v0.5.13: average jitter with aligned inputs

`learning_average_minutes` is an options-flow tunable (1–60 minutes, default 10).
Every integrated observation uses a time-weighted **mean**, with last reported
values held until the next event, including quantized report-on-change sensors.
Control retains its time-weighted median. The coordinator passes raw event filters
to Learning instead of deleting baselines whenever `blocked`/`contaminated` is set.
The zero-window compatibility mode retains its previous quarantine behavior.

Let A be the averaging span, W the learning window, and t the current pass.
The endpoints are means over [t−W−A,t−W] and [t−A,t]. These trailing means describe
temperatures at approximately t−W−A/2 and t−A/2. Integrate **all** regressors over
[t−W−A/2,t−A/2]: actual effort, measured sources, outdoor, solar, interactions,
and the historical lag-effort term. Outdoor/solar retain their observed interval
values; effort/source transition boundaries retain exact overlap weights. Indoor
context uses the same aligned mean. Thus smoothing does not repeat v0.5.11's
unaligned-median delay. Startup/reload/pause refill W+A minutes of observations;
coefficients and covariance survive tuning/reload. Unobserved gaps never join.

Per sensor, retain 31 absolute short-term deviations about the rolling ten-minute
median. Their median estimates normal jitter, with a 0.4 °C floor and a 2% move
toward that estimate per eligible report (reports at most five minutes apart).
Reports outside the current excursion threshold and pending excursions cannot
inflate this estimate. Persist the scale and bounded deviation history. Detection
requires movement beyond **4 × jitter scale**, as well as the configured rate
allowance (one to two minutes, capped so quiet report-on-change periods do not
hide a new excursion). The default amplitude floor is therefore 1.6 °C, admitting
normal ±0.5 °C alternation and ordinary quantization without pending/rejected flags
or “sensor transient ignored” text. No sensor identity enters this logic.

Retain separate learning masks after the control hold expires. A return closes
its original mask and cannot open a false reverse excursion. Each mean omits only
masked time and renormalizes by valid duration. The integrated target sums mean
differences over clean spans, and regressors omit those same physical spans.
More than W/4 total excursion time in a window withholds that observation; a
shorter excursion does not reset its history. Pending time is conservatively
masked too. A sustained new level can rebase after the bounded retention horizon
(W+A+5 minutes in the coordinator); masks cannot suppress learning forever.
Unknown/missing values remain missing rather than being imputed. Legacy saved
filters without scale/history/masks load using the floor and migrate any existing
pending/rejected spans. Restart baseline refresh preserves the learned scale.

The raw-style night acceptance reconstructs two-minute Parent and Wesley/Gabriel
reports from `night-1104.txt`, alternating +0.5/−0.5 °C around each exported value.
Both complete **162** windows, with **7.653** Bryant effective samples and no jitter
transients. Bryant steady effects at 11 °C outdoor are **1.275 / 1.659 °C/hour**,
respectively, versus independent OLS **1.344 / 1.774** (within the existing 35%
acceptance). A 3 °C ten-minute excursion is flagged and masked while learning
continues; a thirty-minute excursion withholds the affected sixty-minute window.
Additional regressions check all-input half-span alignment, last-value quantized
means, slow noise-scale adaptation, old state loading, coordinator explanation
text, and refill after reload. The delayed-system/source simulation now maintains
a physically continuous temperature trajectory so averaging has meaningful time
endpoints. The older defrost replay admits smaller movements inside the new
jitter floor while continuing to flag its large excursions and preserve control
and fan acceptance limits.

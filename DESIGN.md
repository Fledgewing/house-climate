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
  `max_rate` 0.5 °C/min, prediction horizon 45 min, own-sensor `run_margin` 1 °C, fixed `on_margin` and `off_margin` 3 °C,
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
3. Joint allocation. The House setpoint S is the explicit center of the default
   `objective: "band"`. Count only reporting rooms in the active priority mode whose
   weight share is at least `minor_weight_share` (default 5%). Let q_i = w_i / w_max
   over those counted rooms, and n be their count. Minimize:

   `B(u) = max_i q_i |T_hat_i − S| + 0.1 × Σ_i q_i (T_hat_i − S)² / n
   + switch_penalty × Σ_j 1[on(u_j) ≠ committed_on_j]
   + effort_change_penalty × Σ_j |u_j − previous_u_j|`.

   The first term is the smallest weighted band centered on S that holds the counted
   forecasts. The squared term breaks close ties; switching and effort movement
   retain their existing costs. Minor rooms still contribute to house mean/spread
   reporting and learning, but cannot widen this band or defeat the comfort hold.

   Options also offer `objective: "variance"`, retaining the previous objective:
   `J(u) = Σ_i w_i (T_hat_i − T_hat_mean)² + λ (T_hat_mean − S)²`
   plus the same switching and movement costs. For J, normalize all active positive
   weights to sum to one and use `average_weight` for λ (default 1, options 0.01–10).
   Lower λ prioritizes spread and permits a mean below S in heating. The fan override
   remains independent. `learning.forecast` propagates each interval model
   `ΔT_i = a_i Δ_i + Σ_j (b_ij + g_ij Δ_i) u_j + Σ_k (e_ik + g_ik Δ_i) source_k + c_i solar + d_i`
   to the horizon (default 45 minutes), holding current outdoor, source states and solar
   constant. Here Δ_i is outdoor minus the room temperature at the start of the horizon.
   Hold Δ_i in the interaction terms while propagating passive losses every step; the forecast stays affine in effort.
   Missing context omits that term. All nonnegative b coefficients use the existing
   optimistic confidence blend, preserving their physical scale rather than normalizing
   each system's room effects. Without learning, use the same neutral b prior for every pair.
   No room mappings, individual room targets or overshoot ceilings enter the objective.

   Solve using up to 200 cyclic coordinate minimizations, stopping when effort
   changes fall below 1e-9. Evaluate the band cost directly at candidate efforts.
   On/off units consider 0 and 1; modulating units consider feasible endpoints,
   previous effort, the existing quadratic stationary candidates and a 0.05 grid.
   J retains its quadratic stationary points on either side of the movement kink.
   The switch cost makes this a coordinate solution, not a claim of global
   mixed-discrete optimality. Heat efforts are 0..1; cool efforts −1..0. Without
   control state, switching/movement costs are absent; J retains its original
   continuous convex forecasting path. Outage rollouts evaluate the selected
   objective against each candidate's simulated thermostat trajectory.

   A commanded or reported running unit inside `min_run_minutes` has magnitude at least 0.001;
   an idle unit inside `min_off_minutes` or at `max_starts_per_hour` has magnitude zero.
   Starts expire from the rolling window exactly one hour later. If every counted
   room's filtered reading is inside setpoint ± `comfort_band/2`, retain the
   committed on/off state as another allocation bound. Otherwise leave state changes to the selected objective,
   even when the average itself is comfortable. This band never selects heat/cool mode.
   A system with a fine target step (<0.5 °C by default) can modulate; otherwise evaluate
   only full-on/off efforts. Per-system `modulating` overrides that heuristic.
   Unavailable systems have no variable. With no learned positive reach, the thermal objective cannot justify a new start;
   cycling/comfort holds and the state-change cost still apply to a running actuator.

   Under `variance` only, a persisted mean-error integral biases S by at most ±1 °C.
   The band objective neither applies nor updates this bias because S is explicit.
   The variance objective's separate `mean_integral_gain` defaults to 0.001 per scheduled interval, multiplied by
   `average_weight / (1 + average_weight)`; immediate requests do not integrate.
   This smooth gain scaling weakens mean correction along with the mean objective
   (effective gain 0.0005 at λ=1, 0.000167 at λ=0.2). It does not alone eliminate
   long-term bias: the existing reachability gate below remains essential. Conditional integration stops increasing the bias when the predicted mean
   cannot follow it in the requested direction (0.05 °C² tolerance). Old per-system PI
   integrals remain loadable but are unused. This keeps upgrades from applying old PI
   gains or room errors to the joint objective.

   For modulating systems, translate positive effort magnitude q to
   `own_current_temperature + direction × run_margin × q`, at least one device step
   past the reading when required on. For non-modulating systems, use the fixed
   `own_current_temperature + direction × on_margin` (default 3 °C), rounded away
   from the reading to the device step and clamped to device limits. Recompute it
   each pass while commanded on, including steady tests; a minimum-run allocation
   floor never shrinks that clearance.
   Zero effort requests `own_current_temperature − direction × off_margin` (default 3 °C, fixed and never learned). Thus low continuous
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
   suspends this remainder until normal allocation resumes. Non-modulating tests
   refresh the same fixed on margin each pass.
   Dither applies only to positive effort; off targets discard rounding carry and round away
   from demand. Inactivity excitation still applies, bounded in effort/device space.

   `last_decision` names each system's on/low effort, the rooms its learned reach can lift/cool
   toward the setpoint (the mean under `variance`), and the room above/below that
   reference driving restraint. Room and device
   friendly names are used when available; the implementation contains no house-specific names.
   In v0.5.7, explanation room names use active-priority weights: rank the restraining
   room by weight × directional deviation, and helped rooms by weight × reach × deviation.
   Never name rooms whose share of the available objective weights is below
   `minor_weight_share`. For the band objective these same counted rooms also determine allocation and comfort holds.
4. Run the fan/vane scheduler described below, adding at most one changed fan/vane command.
   Write only what changed, except that every non-off `climate.set_hvac_mode` command
   is followed in the same pass by `climate.set_temperature`, even if the reported target
   matches: that report belongs to the old mode. This also applies to immediate House
   mode/setpoint requests; finishing-run and target holds apply only within the same mode. For a system with a
   configured required preset, re-assert that preset if it has left it.
5. Track each system's last successfully commanded mode and target. Report an outside change
   in `last_decision` only when the reported mode/target differs from that command and more
   than 2 minutes have passed since it, suppressing delayed device echoes. The next interval
   re-asserts (the House thermostat is the one control). Check changed fields independently:
   an acknowledgement of the commanded mode with a stale old-mode target is not an outside change.
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
System/source diagnostic effects are converted only for reporting by `60 / interval_minutes`: ×12
at five minutes and ×15 at four minutes. The top-level `units` attribute is
`{"effects": "°C per hour of full effort", "confidence": "%"}`. Internal coefficients,
persisted storage and control maths keep their existing interval units; decision text is unchanged.
Its `systems[system_entity][sensor_entity]` attributes expose fixed `b` (°C/h), interaction `g`
(°C/h per °C of outdoor-minus-room temperature), `steady_std` (°C/h),
`h` (delayed effect, °C/h), `blended_b` (blended fixed part plus g × current Δ plus h, °C/h),
clamped `effective_effect` (°C/h), dimensionless normalized `influence` (sums to 1 for positive reach), confidence percent and `effective_samples`. Its `sensors[sensor_entity]` attributes
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
(0/1 or kW, null for unknown). Reported `e`, `h` and signed `effective_effect` units are °C/h at full-on or °C/h per
kW; the stored coefficients remain per interval. Overall confidence remains the HVAC-pair mean used previously.

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
error cannot establish a win: as of v0.5.20, each winning block also needs an
absolute improvement greater than 0.001 × interval_minutes °C/interval
(0.005 °C at five minutes). Evidence horizons round up
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
the current outdoor sensor/weather fallback and current filtered room readings. Reported slope units are
°C/h per input unit per °C of outdoor-minus-room difference; fixed/effective units match the input's existing
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
As of v0.5.18, qualifying steady normal runs and measured ceiling aborts postpone
eligibility. A short reported run or nonzero allocation cannot veto a due test.
Prefer idle candidates when several are due, then rank by uncertainty. Check each
candidate independently; hand control, unavailability, missing local temperature,
cycling protection, mode and device target limits still apply. Failed starts remain
due on the next pass. Increasing the existing exploration setting tolerates less
remaining uncertainty; zero disables exploration. Whole-system runtime confidence
does not gate tests. Rank named rooms by their score contribution and name the top
two in `last_decision`.

Tests require a counted room outside setpoint ± `comfort_band/2` on the helpful side
of the current mode: below the lower edge in heat, above the upper edge in cool.
A comfortable mean cannot prevent a test for a cold/hot counted outlier. Inside the
band, retain the committed on/off state without starting an experiment. Outside it,
a due uncertain unit receives a full `excite_minutes` test (default 60 minutes) even
when allocation is zero. The normal wait is `excite_after_minutes` (default 120).
One global test runs at a time. A qualifying allocator or experimental run requires
matching reported HVAC action with effort stable within 0.05 for the full duration.
Short runs never reset that evidence clock. With no prior test, initial observation
starts the wait. Restarts clear open run endpoints and retain eligibility clocks.

Forecasts cannot refuse a test start or abort an active test. Instead compare each
counted room's **filtered measured reading** with its anchored ceiling on every pass.
The heat ceiling is S + `test_overshoot` (default 1.5 °C); a room already above that
at test start gets its starting reading + 0.3 °C. Mirror both rules for cooling.
Only a strict measured crossing aborts, regardless of predicted contribution or
passive drift. Do not abort before the unit's configured minimum run, including
later reported starts and cycling protection. Stop the tested unit on the abort pass.
After such an abort, record `last_test_attempt` separately from qualifying steady
evidence and set `next_eligible = abort_time + excite_after_minutes`. This persisted
clock prevents immediate repeat attempts. An unstarted test or other interruption
without qualifying evidence remains due. The existing two-duration wall timeout
bounds attempts without enough reported steady runtime. Learning continues to use
reported action, including a thermostat finishing its own run after an off request.

`sensor.house_learning.experiments` exposes each uncertainty score, last qualifying
steady test, next eligible time and `skip_reason`. Measured aborts include `blocked_by`
(room entity ID), `blocked_reading` (renamed from `blocked_forecast`) and
`blocked_ceiling` (absolute °C). `skip_reason` and `last_decision` name the friendly
room, for example “reading beyond test ceiling: Parent Room”. Other reasons include
“cycling hold”, “not uncertain enough” and “no counted room needs heat” (cooling mirrored).

Cycling constraints retain precedence. One global test runs at full steady effort, with no
dither on any system and no mode selection caused by a test. A due test outside the comfort band overrides balance
allocation; hand control, availability, mode, house demand and safety
still gate operation. Targets track the own sensor as needed to retain full effort.
Complete after the configured observed steady duration; interrupted runtime does not
accumulate across stops. A two-duration wall timeout bounds failed attempts.

Eligibility is the later of the last measured ceiling attempt and the last qualifying
steady evidence time (or initial observation if none), plus `excite_after_minutes`.
Legacy `last_excite` interruption timestamps do not affect it. Skipped, timed-out,
disabled or restart-cancelled tests without qualifying evidence remain due. A measured
ceiling abort after minimum run earns a new wait. No system or room identities enter
this policy.

The seeded delayed-house regression uses independent on/off efforts, a pure eight-interval
(40-minute) transport delay, and true steady response 0.300 °C/h. After 12,000 five-minute
samples, v0.5.8 same-interval RLS learns 0.01531 °C/h; v0.5.9 learns 0.29402 °C/h. Its
120-minute forecast makes the allocator use A for D. Separate controller tests create repeated
five-minute runs that never qualify, then verify a 60-minute constant experiment, increased D
confidence, room safety in heat/cool, measured ceiling aborts, cycling, mode guards, source history,
missing/restart coverage, horizon affinity and lossless main/challenger/library migration.


### v0.5.10 validation (historical forecast gates, superseded by v0.5.18)

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
(default 0.05 °C²) plus the L1 movement cost. If every counted room is inside
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


## v0.5.14: tests must supply evidence without unrelated room vetoes

The forecast veto and immediate safety retry described here are historical;
v0.5.18 replaces them with measured ceilings and attempt cooldowns above.

Regression tests mirror safety checks in heat/cool: a room 3.7 °C beyond setpoint with
0.007 °C/hour unit reach and independent upward drift permits a test, while a strongly
warmed room crossing its ceiling records the room and forecast. Safety skips and aborts
retry immediately once safe. With house error 0.4 °C and balance allocation zero, two
successive 60-minute tests complete, with the next start due 120 minutes after qualifying
evidence. Short reported/allocated operation cannot suppress a due test.

The November 5 19:14 replay uses all seven supplied temperatures and only the Bryant
`climate.home_zone_1`. Priority weights and the fitted live model were not supplied:
weights reproduce the stated 21.6 °C mean, representative under-learned effects leave the
balance allocator at zero, and Office drift is 0.3 °C/hour with the supplied 0.007 °C/hour
Bryant reach. `Learning.forecast` yields 26.007 °C after 60 minutes versus the old 26.0 °C
ceiling, demonstrating an Office veto under the old code and a test start with the fix.
This is a conditional reconstruction, not a claim to recover the exact live forecast.
At the supplied 25.7 °C starting temperature, the old Office gate would block whenever
its one-hour baseline drift plus test effect exceeded 0.3 °C (approximately baseline
rise >0.293 °C using the stated reach and no passive-retention/lag correction).


## v0.5.15: fixed off margin and unexpected starts

An off target was relative to the own sensor with only `run_margin` clearance and a
final one-step off edge. A hot own sensor falling between passes could cross that
old target and start the thermostat without any House demand transition. The
November 5 22:13–22:28 sequence exposed this gap. Dither already could not promote
zero effort, but the instantaneous edge did not protect against later sensor movement.

`off_margin` is a Tunable, default 3 °C, placed below the own reading for heat and
above for cool. It is fixed, never learned. Off targets ignore dither and rounding
carry, round away from demand, and clamp to device limits; if the target cannot be
strictly on the off side, use HVAC off. Positive-effort targets retain `run_margin`.
The margin protects against normal between-pass own-sensor movement; device limits
and movement beyond the margin remain physical constraints.

A reported start while the command is off counts in the rolling start quota and
is marked separately from a commanded start. The coordinator requests an immediate
control pass on that action transition. A start inside the previous minimum off
period is stopped regardless of current allocation. Otherwise allocation decides
whether to adopt it; an unwanted start receives the fixed off target and the note
“unrequested start stopped”. It never receives an unwanted minimum-run hold.
Adoption does not double count the observed start. The marker survives Store state
and clears on a known stop or adoption. Pending commanded starts with known idle action reports retain their on
hold until acknowledged, then minimum run is timed from the later reported start.
Explicit House off and outdoor veto retain precedence.

Regressions replay the four own readings at five-minute passes in heat and cool,
with a thermostat comparing target against own and reporting one pass late. They
check no unintended demand, injected unwanted/too-soon starts and counting, device
limits, and commanded-start lags of five and fifteen minutes. The existing noisy
cycling and November 4 recorder replay enforce runs >=10 minutes, off periods
>=5 minutes and <=3 starts per rolling hour.

Devices without known action reports retain command-clock minimum-run protection.


## v0.5.17: on clearance and units finishing their own runs

The November 6 02:49–03:09 reports exposed insufficient on clearance: normal
positive effort used `run_margin × effort`, and the final required-on edge only
kept a target just past the current own reading. A cloud-polled thermostat whose
sensor rose about 1 °C per pass could reach an older target and stop itself before
House Climate's ten-minute minimum run elapsed. Non-modulating targets now use
fixed `on_margin=3.0` °C, configurable beside `off_margin`, in normal allocation,
cycling holds and steady tests. Round away from the reading, clamp to device limits,
and refresh every pass. Targets that already match need no duplicate service call.
Modulating targets retain effort scaling and rounding feedback, with at least one
step of required-on clearance. Device limits can still prevent full clearance;
between-pass movement beyond the margin remains a physical constraint.

Commanded demand determines hysteresis, comfort holds and start reservations;
reported transitions extend minimum-run timing. Reaching the target does not
change commanded demand: allocation or a hold chooses whether to stop.

A continuing reported heating/cooling action after an off command is the unit
finishing its own protected run, even twenty minutes later. It earns no new start
or minimum-run hold, receives no extra commands, and retains its off target while
House demand remains off. `last_decision` says “<unit> finishing its own run”.
Persist the off target across reloads. Learning continues to use reported action.
The v0.5.15 unrequested-start marker applies to a new known idle/off-to-active
transition while commanded off, not a continuing active report.

Regressions replay 02:44–03:10 in heat and cool at five-minute intervals with
one-pass-late reports and 1 °C own-sensor movement per heating/cooling pass:
one continuous run of at least ten minutes, no on/off/on, and refreshed 3 °C
clearance. Further tests cover both modulating overrides, away rounding, device
limits, steady-test targets and twenty minutes of protected run completion with
unchanged start counts and no commands, followed by a true unrequested start.


## v0.5.18: centered band and measured test ceilings

The owner's rule is: "I want the lowest temperature range across the house centered on my chosen
temperature. If we're there then fine, do nothing. Otherwise it has to test."
The default objective is now `band`; the previous J remains selectable as `variance`.
Counted rooms consistently govern the objective, comfort hold and experiment demand/safety.
A minor Office reading cannot defeat the hold or block a test. Experiments start without
forecast refusal, and measured ceiling aborts after minimum run consume that period's attempt.

`tests/test_band.py` reconstructs the November 6 13:03 heat snapshot and its cool mirror.
The supplied snapshot gives a confidence range, not individual counts, passive coefficients
or lag history. The fixture explicitly assumes confidence 6/4/4/12/2/2% for
Living/Downstairs/Guest/Parent/Annabelle/Wesley-Gabriel, zero passive/lag terms, and
unobserved minor Office/own rows. It retains the existing optimistic system-mean blend.
The 45-minute counted weighted maximum deviation is **1.67000 °C off** versus
**1.56232 °C on**: band requests Bryant on; variance requests off. This is a conditional
reconstruction, not the exact saved live forecast. Applying the supplied raw effects
without confidence blending instead gives **2.01250 °C on**, so those raw rates alone
do not imply the requested allocation. Another confidence assignment within the supplied
2–12% range gives 1.86524 °C on and chooses off, confirming that the range alone cannot
recover the live decision. Separate regressions record both distinctions.
The due experiment starts in either case despite Parent's >23.5 °C forecast, waits
through minimum run and aborts only on a filtered reading strictly past 23.5 °C
(mirrored at 20.5 °C in cool), with another 120-minute wait.


## v0.5.19: mode targets and retained test stops

A device can retain separate heat/cool setpoints. Comparing its old-mode target
with the new target could skip the temperature command after a mode switch,
leaving the device at its stored setpoint until the next pass. Every non-off
mode command now includes the computed target afterward, including immediate
House changes. Same-mode finishing runs keep their existing protection; an old
mode's target cannot suppress or replace the new target. Delayed mode
acknowledgements carrying an unchanged old target do not count as outside edits.

Measured ceiling stops retain `blocked_by`, `blocked_reading`, `blocked_ceiling`
and `blocked_at` (epoch seconds at the actual stop) in `experiments` across later
passes and state roundtrips. These diagnostics clear only when that unit starts
its next test. Current eligibility and skip reasons continue to refresh each pass.


## v0.5.20: learning survives heat/cool transitions

House `heat_cool` is a permanent operating mode, not a learning reset. Reported
heating remains positive effort and cooling negative effort. The controller's
reported effort and thermostat latch follow actual action; they are control
estimates, separate from the thermal learner's evidence eligibility. A same-mode
unit finishing an off request still supplies valid signed runtime.

The coordinator now supplies reported and last commanded device modes to the
runtime integrator, including callbacks under the command lock. An actual mode
change invalidates that interval. Before sending a mode command, mark runtime
unknown; until the reported mode acknowledges it, or while heating is reported in
cool mode/cooling in heat mode, withhold learning. Both the aggregate effort and
the entire interval's transition history are missing, so integrated observations
and lag features cannot silently reuse invalid spans. No coefficients, covariance
or counts are changed by missing evidence. Integrated windows refill with clean
observations; unknown lag columns remain frozen until their own history refills.

Previously these contexts entered ordinary RLS and challenger scores. A sign
change alone is physically valid and does not reproduce the live collapse in a
clean first-order simulation. The supplied rates/times do not include the saved
model or full room readings, so the exact live numerical trajectory cannot be
reconstructed. The regression states its physical assumptions explicitly.

A challenger covariance reset retains its means but now clears all comparison
blocks. Identification still requires a full stable evidence horizon. Fractional
activity counts for overlapping windows now use the same evidence weight as the
main row; the challenger retains its one-day elapsed-time forgetting horizon so
persistent physical changes remain learnable. Main forgetting is unchanged and
never runs on excluded intervals. Confidence in the integrated estimator depends
on the squared steady effect relative to posterior variance, as well as runtime;
a near-zero fitted effect can have very low confidence despite nonzero counts.

Relative prediction wins alone previously allowed tiny correlated averaging or
rounding errors to justify a switch to a fast fit with much greater uncertainty.
A winning block must now improve mean absolute error by more than
0.001 × interval_minutes °C per configured interval (0.005 °C at five minutes),
as well as the configured relative margin. Existing full
coverage, day/night, on/off, stable-time and sustained-win requirements remain.
Promotion archives the full established row, including covariance and counts;
resetting a challenger never changes the main row. Large persistent changes can
still qualify a replacement, and all clean ordinary operation continues fitting.

Store schema `learning_version=2` adds residuals and disturbance streak/status to
the existing exact row/challenger/library persistence. Runtime baselines, integrated
windows and context observations deliberately restart, with no inferred downtime.
Legacy upgrades retain all main and archived rows. If retained four-hour (or
configured return-horizon) prediction evidence already qualifies an archived row,
restore it immediately on load. Otherwise retain the active fit. Seed the legacy
challenger from the retained active fit and clear legacy comparison evidence;
those scores used different activity weighting and may straddle a covariance
reset. This migration is one-time; subsequent Store roundtrips preserve learning
exactly. There is no hidden alternate main row: a replaced row is recoverable only
if it remains in the bounded library, and automatically selecting it requires
observational support. Evicted or overwritten coefficients cannot be recovered.

`tests/test_mode_learning.py` fits eight rooms with the supplied Bryant rates,
then replays 22:11/23:22/00:05/01:23 PT transitions at five-minute intervals (split
at the exact event times), a Store restart at 23:22, and 2.5 days of two-hour
heating/idle cycles plus 60-minute tests every 120 minutes. Effects stay within
20% of pre-switch values and mean confidence loses at most 0.5 percentage points.
This synthetic replay includes a small transitional coasting load, not recorded
room temperatures. Separate tests halve effects for eight days, check both
signed actions, between-tick mode changes, covariance resets, insignificant
challenger wins, disturbance roundtrips and conditional legacy recovery.
`test_mode_replay_through_real_store_restart` exercises HA Store save/load and
coordinator reconciliation with exactly equal saved learning state.


## v0.5.21: closed-loop bias and background drift

Live diagnostics on 2026-11-20, 11 days after a learning reset: the Bryant's fixed
effect `b` re-learned to about 0.5 °C/h in every room (1.06–1.75 °C/h before the reset),
positive interaction `g` and negative lag `h` cancelled it at the cold-evening
outdoor-minus-room difference, effective effects were 0–0.24 °C/h and overall
confidence 1.8%.

`tests/test_closed_loop_learning.py` separates two candidate causes with one synthetic
plant: six rooms heated at 1.0–1.5 °C/h, outdoor 5–10 °C, rooms 20–24 °C, 0.1 °C sensor
steps, short on/off cycles when the house mean is cool and four-hourly tests cut short
when the warmest room passes its ceiling.

- **Low excitation alone is identified.** Every effect is within 10% of truth after 11
  days and `g × Δ + h` stays under 10% of the effect. The nearly constant gap, short runs
  and cut-short tests do not make `b`, `g` and `h` collinear enough to cancel.
- **Closed-loop drift reproduces the collapse.** Add a shared, persistent unmodeled heat
  input (AR(1), about 2.75 h, ±0.2 °C/h) that the loop reacts to. Effects fall to
  0.07–0.36 of truth, `b` to under 0.6 of truth, `g > 0`, `h ≈ −0.18 °C/h`, and
  confidence stays near 2%: the live signature. Pinning `g` and `h` to zero still leaves
  0.16–0.42 of truth, so the bias is in `b`; regularising the corrections cannot fix it.

Root cause: integrated rows model background drift `d` as a constant with the tight
`BACKGROUND_PRIOR` (0.0001, `learning.py` `neutral_row`). The heating decision depends
on the same slow unmodeled heat that the model omits, so the unit runs when the house is
losing heat faster than modeled, and that persistent loss is attributed to the unit's
effort and its interaction and lag columns. This is the classical bias of direct
closed-loop identification with a white-noise model. Confidence cannot flag it:
`confidence()` caps runtime evidence by `effect² / (effect² + steady_std²)`, so a biased
near-zero effect reads as low confidence, and `steady_std` comes from the unit-noise RLS
covariance (it reads 11.6% even in the clean simulation where every effect is within 6%).

`learning_drift` (constructor `drift_noise`, default 0) makes the integrated background
a local level: after every accepted main or challenger fit, its covariance diagonal grows
by `drift_noise × evidence_weight`, i.e. `drift_noise` RLS units per learning window.
Coefficient means, other covariance entries and the per-interval compatibility mode
are unchanged; Store keeps the setting and old stores load with 0. Changing the option
keeps every fitted row. Measured on the same plant at 11 days with `learning_drift=1`:

| case | effective / truth | notes |
| --- | --- | --- |
| closed-loop drift | 0.81–0.85 | `g × Δ + h` under 10% of the effect |
| drift + 15-minute transport delay | 0.88–0.90 | `h` 0.30–0.44 °C/h is still learned |
| true weather slope 0.018 °C/h/°C, ±8 °C swing | 0.95–1.01 | `g` shrinks to 0.002 °C/h/°C |
| slope + drift | 0.81–0.88 | |
| clean | 0.93–0.95 | confidence 8.7% |

The default stays 0 because the random walk also discards slow exogenous variation.
With 1, four existing regressions fail: the white five-minute effort/40-minute pure-delay
fit learns 0.12 of 0.3 °C/h, two Night 11-04 recorder replays fall outside 35% of the
level-based least-squares reference in at least one room, and the 8-day halved-effect replay
adapts too slowly (0.78 versus 0.575 ± 0.115 °C/h). Residual-persistence and closed-loop
detectors that switch the drift on automatically were tried and did not clear both
groups. Confidence is unchanged; scaling `steady_std` by the measured fit error would
read 66% for the biased default fit and 86% with drift, so it is not a safe signal on
its own.

From a reset model with `learning_drift=1`, effects reach 0.82–0.87 of truth after one
simulated day. Enabling it on an already biased 11-day fit barely helps (0.15–0.41 of
truth after seven more days), because that fit's covariance is already tight; it needs a
learning reset to benefit.

Experiments: a test cut short by a measured ceiling already contributes evidence, because
learning integrates reported action whatever the test state. The limit is selection: a
test starts only when a counted room is cold and stops when one is warm, so test timing
is also part of the closed loop. No experiment policy changed.


## v0.5.22: local run sensor and duct probes

Live evidence 2026-11-21: `climate.home_zone_1` (Bryant Evolution Connex via the ha_carrier
cloud integration) reported `hvac_action: heating` for 76+ minutes while the same snapshot
said indoor unit off, airflow 0 and outdoor unit Stage 0; status changed only on hourly full
refreshes or reconnects. Rooms fell while it said heating and rose while it said idle, and
every learned effect collapsed to ≤0. No learner change can recover a sign that the input
reports backwards, so the fix is a better input.

Each system may set `run_sensor` (binary on = running, or numeric with `run_threshold`,
default 50, compared in the sensor's own unit; kind is inferred from the domain, as for
extra heat sources). `learning.run_action(running, mode, active_mode)` maps it to the
signed action: heat → heating (+1), cool → cooling (−1), not running → idle (0). In device
`heat_cool`/`auto` the direction is `ControlState.active_mode`, the controller's selected
direction and the mode it commands; with no heat/cool direction, or running while the device
reports off/fan/dry, the action is unknown. The coordinator's `learning_action` feeds that
action to `EffortTracker` on device events, run-sensor events (`_run_changed`) and each
tick, with the existing mode/commanded-mode checks, so the v0.5.20 transition withholding is
unchanged. An unavailable/unknown run sensor reports `None`; one whose `last_reported` is
older than `stale_after` also calls `EffortTracker.invalidate`, so the whole interval and
its transition history are missing, as for a mode transition, and nothing falls back to the
device's action. Measured efforts passed to the controller (`drive_bias`) come from the same
tracker. Control estimates (`observe_report`, cycling clocks, thermostat latch) and fan
windows still use the device's report: the option is scoped to thermal learning evidence.

`run_sensor`/`run_threshold` live in the system's options and are absent when empty, so
existing entries are unchanged. Options changes reload through the Store, which keeps every
learned row (system list unchanged). `climate.house` `action_sources` shows per unit whether
the action came from `run_sensor` or `device`, with the run state, problem and the device's
reported action.

`tests/test_run_sensor.py` replays the 11/21 pattern (actual hour on/hour off, cloud
reporting the inverse) for three days: with the run sensor every room's effect is within
20% of the supplied Bryant rate, without it every effect is ≤0 (today's behaviour). HA
tests cover binary and power sensors overriding a contradictory device action in heat and
cool, unavailable and stale run sensors yielding no evidence, and options set/clear keeping
learning.

### Duct probes: signed, continuous effort from ΔT

The Bryant's blower runs almost continuously, so a run sensor on the blower says nothing and
one on the compressor circuit misses strips on another circuit and gives only on/off for a
variable-speed unit. Supply − return air temperature gives both direction and size. Each
system may set `supply_sensor` and optionally `return_sensor` (temperature sensors). Without
a return sensor the return is the system's own climate `current_temperature`, the
thermostat's indoor reading: always present, near return-air temperature, and needing no
extra choice; a room sensor can be picked as `return_sensor` instead.

`learning.delta_effort(ΔT, delta_deadband=2, delta_full_heat=15, delta_full_cool=10)`: |ΔT|
within the dead band is 0; otherwise ΔT/`delta_full_heat` above zero and ΔT/`delta_full_cool`
below, clamped to [−1, 1]. Proportional to ΔT rather than to ΔT minus the dead band, so a
part-load ΔT maps to its capacity fraction at constant airflow (the always-on blower). The
clamp stays at ±1: the linear learner would accept larger values, but a system's effect is
per full unit capacity, `ControlState` efforts and `drive_bias` are capped at 1, and strips
beyond a full ΔT would otherwise inflate the per-effort effect. The sign is the air's, never
the device mode's: a defrost in heat mode is negative and a stale cloud mode cannot flip it.
One coefficient per system serves both signs, so the two scales also define how much cooling
equals one unit of heating; a defrost's negative ΔT uses the cool scale and is overstated by
15/10, which is small because defrosts are short.

The coordinator samples ΔT (`delta_state`) on each update of either probe and of the system
entity (fallback return) via `_local_changed`/`_system_changed`, and on each tick, and feeds
it as a numeric action into the same `EffortTracker` that modulating units use; the tracker
holds each sample until the next and integrates over the interval, so no smoothing is added.
The v0.5.20 mode-transition withholding still applies (numeric actions are never nulled by
the heat/cool mismatch check, only by mode transitions or unacknowledged commands).

Combination rule (`learning_action`):

1. probes trusted → ΔT effort, `source: delta_t`. A trusted run sensor only cross-checks:
   `disagreement` is set when `running != (effort != 0)`, and ΔT still wins (strips on a
   separate circuit, compressor starting);
2. probe unavailable/unknown/stale (same `stale_after` rule as the run sensor, via
   `local_problem`) and run sensor trusted → the run-sensor action, `source: run_sensor`;
3. neither trusted → no action, `problem` set, and the tick calls `EffortTracker.invalidate`;
   a configured local sensor never falls back to the device's reported action.

`action_sources` adds
`supply`, `return`, `delta_t`, `effort`, the sensors used, `disagreement`, and
`delta_problem`/`run_problem`. Fields are absent when empty; clearing `supply_sensor` clears
every ΔT field; options changes keep learning.

`tests/test_duct_delta.py`: the ΔT→effort table; ΔT overriding a contradictory action and
mode (stale cool mode while heating, defrost in heat mode, device off while air is heated);
integration of probe updates within an interval; the run-sensor combinations; unavailable and
stale probes yielding no samples; the return fallback; options round-trip. Replays of the
11/21 pattern with probes (on/off, and a variable-speed unit at 35–85% part load, both with
defrosts) learn every room's per-full-effort Bryant effect within 20% over four simulated
days (observed 0.97–1.06 of truth), while on/off-only evidence for the variable-speed unit
learns under 75% of truth.


## v0.5.23: slow cloud reporters, command-derived and latent effort

Live evidence 2026-11-21/22: `climate.home_zone_1` (ha_carrier) fetched new data about every
two hours (`sensor.living_room_home_all_data_last_updated`: 11:57, 14:43, 16:08, 16:16,
16:21 PT on 11-22), and setpoint writes at 15:05/15:15/15:45 re-published a stale `heating`
at an old temperature, moving the climate entity's `last_updated` without a fetch. Its
indoor/outdoor status sensors are frozen. No local probe is fitted yet. Learning from the
reported action is learning from a snapshot up to two hours old. The approved design is
"effort inference for slow cloud reporters"; this release builds both parts: part A (tasks
1–9: slow tag, command labels, contradictions) and part B (tasks 10–14: window advance and
backfill, count weights, latent effort from the rooms' response). The design planned part B
as v0.5.24; both ship as v0.5.23.

A single missing effort costs about 70 minutes of evidence: `Learning.update` clears all
windows when any system's effort is None, and `averaged_window` needs `window + average`
minutes of contiguous labelled updates. So unclear spans are skipped, transitions are not.
(Part B changes the first half: a missing effort now advances the windows instead.)

### Slow-reporter tag (`reporting.ReportCadence`)

Per system: `report_trust` (`auto` default, `fresh`, `slow`), `report_timestamp_sensor`
(empty), `report_fresh_minutes` (5), `command_effort` (on). Absent keys are defaults, so old
entries load unchanged. A report time is the timestamp sensor's state when set (O2 answered:
the climate entity's `last_updated`/`last_reported` also moves on command echoes), else the
entity's `last_reported`. `ReportCadence` keeps the last 48 gaps between strictly later report
times (echoes of one fetch add none); `auto` is slow when the median gap exceeds 3 × interval,
and slow until 6 gaps exist. It persists in the `reporting` Store key next to
`CommandChecks`; v0.5.22 stores load with both empty. A slow report is fact only for
`report_fresh_minutes` after its report time; the tracker gets a `None` at that expiry
(`label_until`), so a stale action never extends past it.

For a slow, non-fresh system its own climate row is left out of `learning_values` (kept,
not deleted). **Control is unchanged from v0.5.22** (review decision after part B): the
design's Q5 (fresh-only thermostat points and starts) was built in part A by hiding a stale
action from `observe_report`/`observe_action` and from `control()`, but
`observe_action(None)` makes the next fresh report a new start (fake starts, cycling hold,
min-run re-armed, steady-test runtime reset). v0.5.23 is learning-only: control sees the
device's raw reported action, and `report_status` on its events feeds only the cadence.

### Command labels (`learning.command_effort`, `own_estimate`, `CommandTransitions`)

The coordinator records each `set_hvac_mode`/`set_temperature` as `command_settings[key] =
{mode, target, sent_at}` when it is sent (persisted; a failed call restores the previous one).
It is confirmed when the unit echoes mode and target (within max(half a step, 0.3 °C)), or within
`command_confirm_seconds` (120 s) of sending, and no outside change was noted since. The
thermostat's reading is an explicit `own_sensor` room sensor, else the reported
`current_temperature` moved by the house mean's change since that report
(`report_marks[key] = {reported, mean}`, recorded when the report time changes), with the
size of the move as the uncertainty `c`. `command_effort` then gives on when
`d·(T − own) ≥ max(1.5, η + 1.0) + c`, off when `≤ −1.0 − c` or mode off/fan_only, else
ambiguous; `η` is the learned `thermostat_points` mean or `thermostat_hysteresis`.
`CommandTransitions` turns the class into a value over time: on is 0 until
`max(cause + 2 min, last run end + 5 min)` then ±1; off after a run is 0.5·d for 3 min then
0; ambiguous is None. `cause` is the command's send time when the command is newer than the
current label, so a restart with a stored command does not re-apply the start delay.
`feed_effort`/`label_until` insert the transition breakpoints into `EffortTracker`, and
command labels pass `trusted=True`, which skips the mode-transition withholding; a
command-labelled system also skips the `set_hvac_mode` pre-mark and is re-labelled right
after its commands.

Chain (`learning_action`): local probe > run sensor > clear command > fresh report > latent
(`latent_action`, part B) > skip. Non-slow systems without
local sensors keep the exact `device` path. Untrusted local sensors fall back to a clear
command, then latent, never to the report.

### Contradictions (`reporting.CommandChecks`)

Each distinct fresh report is one cross-check against a settled label (≥ 5 min past the
start or the stop tail): reported running vs label on. Over the last 20 checks, with at least
10, a rate above 30% suspends command labels (source falls to report/latent/skip,
`problem: command_contradicted`, repair issue `command_contradicted`); below 10% clears.

### Decisions beyond the design (all reversible)

- `report_timestamp_sensor` option added (maintainer's live O2 answer).
- The cadence is not seeded from recorder: recorder rows exist only for changed states, so
  their gaps overstate any device's cadence; seeding would mark a fast unit slow for longer
  and adds nothing for a slow one (bootstrap is slow anyway). The Store persists the cadence.
- Auto bootstraps slow (Q3), so tests that model locally reporting units set
  `report_trust: fresh` in their fixtures.
- A command is confirmed during a 120-s grace before its echo, and recorded when sent rather
  than after the call returns, so the service call's own echo is labelled, not lost.
- Only an explicitly stored `own_sensor` is used for the command comparison: a sensor matched
  on the unit's own device arrives through the same slow fetch.
- The on gap is `max(clear_gap, η + off_gap)`, so a learned wide hysteresis widens it.
- Contradictions need ≥ 10 checks before suspending (30% of a handful is noise); the 10%
  frozen snapshots of the simulation never suspend.
- The tuning constants live in `const.COMMAND_DEFAULTS` (config keys, not UI), so the tunables
  form and its tests are unchanged.

### Part B: late labels (`Learning.update`, `backfill_effort`, `effort_span`)

In averaged mode a missing effort no longer clears every window: `update` still records the
gap in `effort_history`, appends the interval's context, observes the internal filters and
advances each window's end (`accepted` false). `averaged_window` rejects any window whose
span holds the gap, as before, until `backfill_effort(system, start, end, value)` replaces
the missing segments (only missing ones; known labels are never overwritten) and flags them
`latent:system:<key>` in the segment's dict. Other paths (plain and unaveraged windows, a
missing temperature, outdoor or solar) still clear. `windows[sensor]["accepted"]` now says
whether the last evaluation produced a window; `windows_accepted` (coverage) and the
off-span metric of the simulation use it, since a window's age alone no longer means its
labels are complete.

A latent label is complete only a block plus its response delay after the block, so while
an estimate is ready for some system the coordinator sets `Learning.label_delay_minutes` to
block + longest delay (45): averaged windows are then evaluated that much later (window
`[end − lag − 45 min − 60 min, end − lag − 45 min]`, contiguity `≥ window + average + 45`).
The room filters' history (and the internal filters, context and effort history) grows by
that delay when averaged learning is on. Before a system is ready the delay is 0 and
learning timing is unchanged.

### Part B: no self-confirmation (count weights, independent covariance)

`averaged_window` also returns each input's latent share. `fit_row(..., count_weights)`
scales each input's count update by its weight, in both the increment and the ageing
(`forgetting**w · n + evidence_weight · w · active`). A window holding *any* latent time for
an input has weight 0 for it: the count neither grows nor ages, exactly as if the window had
been skipped. Weight 1 (and no weights) is the old update bit for bit.

The integrated confidence is `min(n/(n+n0), effect²/(effect² + steady_std²))`, and latent
windows shrink the covariance like any other window, so counts alone did not stop
inflation: the simulation gained +0.5 points (drift 0) and +4.7 points (drift 1) through the
second term. Each row therefore keeps `independent`, a covariance updated only by windows
without latent time (None until the first latent window, then a copy). `confidence` uses it;
`steady_std` for control and diagnostics keeps the full covariance. Coefficients learn from
every window. Rows stored by v0.5.22 load with `independent` None (= their covariance).
Row dicts in the Store keep exactly the v0.5.22 `Row` fields (a v0.5.22 rollback builds
`Row(**data)`); the independent covariances are a separate top-level map
`independent_covariances` (`rows`, `challengers`, `saved` by id), re-attached in
`from_dict` (a dimension change drops them). `reconcile` remaps them with the covariance's
index mapping; a challenger covariance reset clears its copy. An input whose lag hour
(the lag regressor's span) holds latent time also counts nothing, in averaged, windowed
and plain fits.

### Part B: latent effort (`latent.LatentEffort`, `latent.room_terms`)

Per tick the coordinator feeds each usable room (not disturbed, temporary, not reporting or
filter-blocked): its 10-min filtered mean, its passive rate (`room_terms`: the learning row
with all systems off: outdoor loss, solar, background, extra sources, °C/h) and every
system's blended hourly effect. On 15-minute blocks (aligned to the clock) for each system
with `latent_effort` on, once the block plus 30 min of response is in:

- **Anchor**: the block's label is known for the whole block, constant and not latent
  (`effort_span`). For every delay δ ∈ {0, 5, 10, 15, 20, 30} min, each room's response
  `r = (T(b+B+δ) − T(b+δ))/B − mean passive − Σ_{k≠j} effect_k · label_k` (others' labels over
  the shifted span; any other system unknown there drops the block) updates weighted sums
  for `r = s·u + c` with forgetting 0.998 per anchor block. On (|u| > 0.5) and off (u = 0)
  anchors are kept for 7 days.
- **Estimate**: the block has missing time, and the system is a candidate (slow without
  local sensors, or local sensors untrusted now). The fit takes the delay with the highest
  mean R² over rooms, rooms with |s| > 2σ. Ready at ≥ 12 on and ≥ 12 off anchors and ≥ 3
  such rooms. With k the known label mean and f the missing share,
  `û = Σ w s f (r − c − s k) / Σ w (s f)²`, `se`, `χ²/(n − 1)`; label 0 when |û| ≤ 0.25, d
  (the mode's direction; heat_cool uses the active mode) when d·û ≥ 0.75, both needing
  se ≤ 0.2 and χ² ≤ 3; else nothing. A decisive label is backfilled over the block's missing
  time and its coverage span is relabelled `latent`.

Latent blocks are never anchors, so shares, offsets and noise come only from independent
labels. Anchors older than 7 days before the latest tick stop counting (readiness
expires on a stale calibration). Persisted in the Store under `latent` (sums, anchors, last
processed block, latest tick, `engaged`, last estimate per system); the tick buffer is not persisted (a restart loses under an hour of
blocks). Stores without the key load empty.

Coordinator: `latent_action` (the part A seam) returns the estimator's diagnostics when
ready, and the chain then reports `source: latent` with action None (the present span is
labelled later). `action_sources[...]["latent"]` carries `u_hat`, `se`, `chi2`, `label`,
`block`, `delay_min`, `anchors_on`, `anchors_off`, `rooms`, `ready`; `coverage_24h` gains
`latent`. Option `latent_effort` (per system, default on) in the system step, strings and
`translations/en.json`; `latent_block_minutes` (15) is in `COMMAND_DEFAULTS`.

### Decisions beyond the design, part B (all reversible)

- **Label delay held once engaged**: the first time any candidate is ready, `engaged` is
  set (persisted) and the 45-min learning delay stays on, even if readiness later lapses;
  it drops to 0 only when `latent_effort` is off for every system. Flipping with readiness
  would fit or skip the same 45 minutes of windows twice.
- **Command echo tolerance** is `max(step/2, 0.3)` °C (part A used step/2): a °F unit
  shown in °C rounds the echo (22.5 °C → 72 °F → 22.22 °C).

- One release: part B ships in v0.5.23 with part A, not as v0.5.24 (as instructed).
- **Count weight is 0 for any window holding latent time**, not the design's
  `1 − latent_fraction`. With the fraction, mixed windows (independent minutes beside latent
  ones, rejected outright in the skipped replay) raised confidence +3.3 points in the
  simulation; the strict rule keeps the no-inflation test exact for counts.
- **Latent time also neither ages a count** (forgetting^0), so a latent-only window is a
  no-op for counts, as in the skipped replay.
- **Independent covariance for confidence** (above): needed because the covariance term of
  `confidence` otherwise rose up to +4.7 points with `learning_drift` 1.
- **Learning label delay** (45 min, only while some candidate is ready): without it, a
  long ambiguous stretch never completes a window, since the newest blocks are always still
  pending; the simulation filled 40% more time with labels but changed no fit.
- `latent_action` cannot label the present (the response is not in yet): it marks the span
  `source: latent` and the backfill labels it; it returns None while not ready (skip).
- The estimate subtracts the block's known part (`s·k`) and scales by the missing share f,
  so blocks that are partly labelled (a command turning ambiguous mid-block) are estimated
  too; small f gives a large se and no label.
- Anchors are gathered for every system with `latent_effort` on (not only slow ones), so a
  unit that turns slow is calibrated already; estimates only for candidates.
- The response delay is chosen per system from the anchor fit (one δ per system), not per
  block.
- Others' effects come from the learning rows' blended effects; their uncertainty is
  absorbed by the anchor offsets and noise. The passive term uses the learning row too.
- Anchor and readiness constants (12/12 blocks, 3 rooms, 7 days, 0.25/0.75, se 0.2, χ² 3,
  forgetting 0.998) are `LatentEffort` arguments, not configuration.

### Tests

`tests/test_slow_reporter.py` simulates three days: six rooms, a latched thermostat
(η 0.5), 2-min start delay, 5-min restart guard, 6-min defrost per 60 min of running below
5 °C, a 3-min warm tail, the controller's own ± 3 °C commands on the reported temperature, a
daily 5-h commanded off span, reports every 120 min at :57 and after each write, 10% frozen
contradictory snapshots. HEAD's path (the reported action, mode withholding, pre-mark)
learns 0.27 of the per-commanded-hour truth in every room (pinned < 0.5). Command labels
learn 1.01–1.03 of truth with `learning_drift` 0 and 1, coverage 99.8%, 100% of the off span
with full windows (HEAD 77%); a variable-speed unit at 35–85% part load learns 1.00–1.01 of
mean delivered per commanded hour; a lockout suspends labels. `tests/test_command_effort.py`
covers the pure pieces and the coordinator: a clear off beating a stale `heating`,
fresh report then skip, a mode switch kept with its start delay, the repair issue raised and
cleared, hand control, persistence and the options round-trip.

Part B: the same simulation with a modulating unit (on targets own + 0.6–1.0 °C at 0.1 °C,
mostly ambiguous; a 2-h clear warm-up after the daily off span supplies on anchors), six
days. With latent labels skipped, coverage is 30.0%; applied, 70.3% (drift 0) / 70.2%
(drift 1): +40 points. Confidence: 8.64% skipped vs 8.66% applied (drift 0; per room
within 0.13 points), 2.234% vs 2.233% (drift 1; per room within 0.10 points); counts are
equal. Effects with latent labels are 1.00–1.02 (drift 0) and 0.98–1.01 (drift 1) of the
per-commanded-hour truth. Of 252 decisive latent blocks, 3 disagree with the plant's
majority run state in the block (1.2%; drift 1: 2 of 251). The estimator is ready from day 2.75. With window advance the binary command
simulation is unchanged to ±0.001 (1.02–1.03 of truth, coverage 99.8%, off span 100%); the
pinned HEAD path learns 0.28 (was 0.27) and keeps 78% of off-span windows (was 77%). `tests/test_latent_effort.py`: window advance
and backfill, `effort_span`, count weights and the independent covariance, the label delay,
v0.5.22 rows loading; on a synthetic six-room house, on/off recovery after anchors, refusal
under 12 anchors, a one-room 1 °C/h spike rejected by χ², û 1 ± 0.15 on held-out known
blocks, a second unlabelled system blocking; and the coordinator: a backfilled heating
span (`source: latent`, coverage, delay 45, persistence), the option off, and a v0.5.22
store loading.

## v0.5.24: slow reporter hysteresis, command margin, attribute snapshots

### Live evidence (v0.5.23, `climate.home_zone_1`, 2026-11-22 18:09–21:54 PT)

Command labels were right when given, but `command_action` turned ambiguous about 30 min
after each report, so no 70-min learning window completed (coverage: command 53%, skipped
42%, windows 0; learning frozen). From `action_sources` history:

- 18:09 command heat 24.5, est 21.50 (report age 130 s) → on; 18:14 est 21.58 → on; … 18:34
  est 21.63 → on; 18:39 est 21.83 (age 1927 s, correction ≈ 0.33), gap 2.67 → ambiguous;
  18:59 est 22.17, correction 0.67, gap 2.33 → ambiguous; 19:14 gap 1.97 → ambiguous, until a
  fresh fetch.
- 20:07 fresh report: heating, reading 24, target 24.5 (it heats with the target only 0.5
  above its reading). 20:09 target 27, est 24.16 → on; 20:14 → on; 21:04 est 24.3 (age
  3421 s) → ambiguous; 21:09 → on (start delay again); 21:15 mode off → stop tail, then off.

### Causes

(a) `η` in `command_effort` was the mean of `thermostat_points`, recorded by
`ControlState.observe_report` as `target − own` at an observed idle→heating transition. A
slow reporter's transition is observed up to 2 h late, so the reading has already fallen
further below the target: inflated points (inferred η ≳ 1.4 °C), on threshold
`max(1.5, η + 1.0)` ≈ 2.4–2.5 °C.
(b) `own_estimate` already moves the reported reading by the house-mean change since the
report; `command_effort` then added `|correction|` again as margin. And that warming is
mostly the commanded unit doing what it was told.

### Fixes

1. **Learning, slow units:** `command_action` takes η from `controller.thermostat_hysteresis`:
   the clean points (below) once there are `fresh_points_min` (4, `COMMAND_DEFAULTS`), else
   `thermostat_hysteresis` (0.5). Threshold 1.5 for the Bryant. Units that are not slow
   (command labels for untrusted local sensors) keep the learned `thermostat_points` mean.
   `command` diagnostics gain `hysteresis` and `hysteresis_source`
   (`fresh_points`/`points`/`default`).
2. **Learning, margin (direction-aware):** `command_effort` takes the signed house-mean
   move since the report (`drift`, from the coordinator as `own − reported` when the
   source is `reported+house`). A move in the commanded direction (warming under heat,
   cooling under cool) is consistent with the command and already in `own`: it adds no
   on margin. A move against it adds `min(command_margin_factor·|c|, command_margin_cap)` =
   `min(0.5·|c|, 0.5)` °C (both in `COMMAND_DEFAULTS`; v0.5.23 was the full |c|, any
   direction). The off test keeps the symmetric margin: a house warming under a heat
   command whose target is already behind the reading moves the estimate further past
   the target, so the margin only delays a clear off; no reason found to change it.
   Without `drift` (unknown sign) the margin is symmetric.
2b. **Learning, running hold (coordinator follow-up):** with the real warming rate the
   estimate reaches 1.5 °C of the 24.5 target by about 19:49 (est 23.07, gap 1.43), and
   the 20:07 fetch showed the unit still heating at gap 0.5. A thermostat that is running
   latches until its reading reaches the target (as `thermostat_effort` models), so a label
   already on in the command's direction stays on while the gap is at least
   `command_hold_gap` (0.5 °C, `COMMAND_DEFAULTS`) plus the on margin. Starting from no
   label or off still needs the clear gap. **Trend condition (review fix):** a satisfied
   thermostat was held on for 70 min (estimate 23.54 lagging the real 24.5). The hold now
   also needs the house mean moving in the commanded direction over the last
   `command_trend_minutes` (15) at ≥ `command_hold_min_rate` (0.01 °C per 5 min; the live
   evening warmed 0.04–0.08 °C per 5 min, so a quarter of the slowest live rate, above
   sensor noise of a multi-room mean). `command_effort` returns `held` for an on that
   survives only via the hold (`trend` and `held: true` in diagnostics); a clear-gap on is
   unaffected. The trend comes from `HouseCoordinator.house_trend` (house-mean samples,
   not persisted: after a restart there is no hold for one window). In the live replay the
   hold carries 19:49–20:04 only.
3. **Display:** `climate.house`'s `extra_state_attributes` returned
   `coordinator.action_sources` (and other maps) by reference. The coordinator mutates them in
   place, so the previous state's attributes held the same objects, HA's `attributes ==`
   comparison saw no change and skipped the write: the frontend kept `{}` while REST and the
   recorder (written on other attribute changes) showed real values. The climate entity and
   both learning sensors now return a `deepcopy`. Switch attributes are scalars.
4. **Control change (user-approved), slow units:** thermostat points are recorded only from
   a cleanly observed transition (`ControlState.observe_fresh_report`, called from
   `_system_changed` before `observe_report`): an idle report followed by a heating/cooling
   report at the same target and mode, both fresh (`report_fresh`, the v0.5.23 report
   freshness), report times strictly increasing and at most `report_fresh_minutes` apart, gap
   in (0, 3]. A command echo (same report time, new target or mode) is not a report of that
   setting and never counts as the idle side. They go to `fresh_thermostat_points` (last 32).
   For a slow unit, `thermostat_effort` (the outage/latch model) uses the same
   `thermostat_hysteresis`: fresh points once ≥ 4, else the 0.5 default. Units that are not
   slow keep `thermostat_points` exactly as before; `observe_report` still appends to that
   list for every unit, so a rollback finds it unchanged.
5. **Learning, continuity (found by the regression simulation):** the coordinator ticks on
   every reported action change. At the 20:07 fetch the command (24.5 vs a fresh reading of
   24.0, gap 0.5) is honestly ambiguous, which dropped the on label; the 20:09 command then
   restarted it with a false 2-min start delay. Now an ambiguous confirmed command whose fresh
   report shows the unit running in its direction is on (`reason: fresh report running`); a
   new on label starts at the report time (the unit was already running then).
6. **Review fixes to the report rules:**
   - The mirror rule: a fresh report showing idle/off while the command is ambiguous or
     held labels off from the report time (`reason: fresh report idle`; stop tail if it was
     on). A clear on stays on and the report is a contradiction check instead.
   - Both report rules trust only a report made at or after the command's `sent_at`
     (an echo of an older fetch says nothing about the command).
   - The cross-check compares a fresh report with the label in effect *before* its report
     time (`CommandTransitions.state_before`), captured before this tick's observe. It used
     the label after this tick's update, so a held on that a fresh idle report ended was
     never counted. "Before", not "at": when the climate entity updates before the
     timestamp sensor, the first tick pairs the new reading with the old report's mark and
     may relabel at the report's own instant. A report within 5 min after the current
     command was sent is not cross-checked (it reflects the change itself, e.g. the
     21:15 off echo-fetch).

7. **Report-mark ordering (fix):** when the climate entity updates before the timestamp
   sensor, the action-change tick paired the new reading with the previous report's mark
   (live 20:07 replay: reading 24.0 + 1.4 °C since 18:07 = est 25.4, ambiguous, effort
   None, averaged windows restarted: the ~70 min loss this release fixes). Chosen: both
   halves. `report_status` remembers when the climate entity's reading changes, with the
   report time and house mean then (`report_content`, not persisted). A reading that
   changes while the report time is one already marked more than `report_pair_seconds`
   (120) ago is *early*: until the report time moves (at most `report_settle_minutes`, 10)
   the status carries `report_pending` and `command_action` keeps the current label
   (`reason: report time pending`) instead of estimating. When the time moves, the mark
   is re-keyed with the mean from when the reading arrived. A reading that changes
   within 120 s after a new report time was first seen belongs to it (timestamp sensor
   first, with a tick in between). Only the reading counts as content (the action does not
   enter the estimate; a mode echo changes it). A command sent after the current label is
   still classified while pending (the mode-switch echo case). An echo whose reading never
   gets a new time is held at most 10 min, then the old mark is used as before. Holding
   was chosen over re-keying alone because re-keying cannot tell a fetch from a stale
   echo until the timestamp moves.
8. **Attribute size (fix):** live HA logged "State attributes for sensor.house_learning
   exceed maximum size of 16384 bytes" every 5 min (113 times on 11-22) and the recorder
   dropped them. A new `attributes.snapshot` deep-copies and rounds floats to 4 significant
   digits (values ≥ 1e6, timestamps, are kept); used by `climate.house` and both learning
   sensors. `sensor.house_learning` exposes per row only `b` (`e` for sources),
   `effective_effect`, `confidence` and `effective_samples` (dropped: `g`, `h`,
   `steady_std`, `blended_b`, `influence`), saved regime models without their
   coefficients, and no per-sensor `disturbed` (duplicate of the top-level map). There is
   no diagnostics download in this integration, so the dropped fields remain only in
   `Learning.diagnostics()` (and the Store). 8 rooms, 2 systems, 4 sources, 3 saved models
   per row: 16.7 KB before, 11.8 KB after (fan learning 0.3 KB, climate 3.0 KB). Each
   further source adds about 1 KB.

### Storage and rollback

`ControlState` is saved as `asdict(state)` and v0.5.23 loads it with `ControlState(**state)`,
so the new per-system maps are attributes set in `__post_init__`, not dataclass fields:
`fresh_thermostat_points` (persisted under its own top-level Store key
`fresh_thermostat_points`), `fresh_reports` (the last report per system, not persisted:
the first transition after a restart is not recorded) and `slow_reporters` (set by
`report_status` on every report, read by control). A v0.5.23 store loads with no clean
points; a v0.5.24 store's `state` has exactly the v0.5.23 field set, and v0.5.23 ignores the
new top-level key.

### Decisions beyond the brief (all reversible)

- Item 5 above (fresh running report settles an ambiguous command): without it the replayed
  evening kept a 2-min false start delay at 20:09 and was not continuously on.
- Clean points are recorded for every unit (cheap) but used only while it is slow, so a unit
  that turns slow already has them.
- The fresh-points minimum is a `COMMAND_DEFAULTS` config key like the margin constants
  (not in the UI).
- `slow_reporters` follows the auto bootstrap: an `auto` unit with fewer than 6 measured
  gaps is slow, so a newly added prompt unit uses the 0.5 default in the latch model until
  its cadence is measured (persisted cadences are unaffected).
- The 19:14 tick (gap 1.97, correction +1.03 °C since the 18:07 report) is on by the
  direction-aware margin alone (threshold 1.5); the hold also covers it. The hold is the
  only way the 19:49–20:04 ticks stay on with the real warming rate.
- The hold gap is 0.5 °C, not 0: the estimate is a house-mean extrapolation, so a small
  safety gap is kept before calling the latch still on.

### Tests

`tests/test_slow_hysteresis.py`: clean transitions record a point; two hours apart, either
side stale, the same report and an unknown time do not; a command echo is never the idle
side; `thermostat_hysteresis` for slow and prompt units and the 4-point minimum; the control
latch ignores inflated points only for a slow unit; the live 18:39 numbers (ambiguous with
the v0.5.23 parameters, on now); the live 19:14 numbers (on with warming drift, ambiguous
unsigned or against). The live evening replayed through the coordinator (reports 17:55,
18:07, echo 18:09, 20:07, echo 20:09, 21:15; the real warming since 18:07, est
21.83/22.17/22.53 at 18:39/18:59/19:14 (asserted), then ≈ 0.077 °C per 5 min; inflated
points [1.5, 1.6, 1.4]; ticks every 5 min plus the action-change ticks):
command on (`transition: on`) at every tick 18:14–21:14, effort 1 from 18:19 (18:14 holds the
start delay), off after 21:15 plus the tail, effort 0 from 21:24, at least one learning
window accepted, the legacy list still gaining 0.5 at 20:07 and no clean point. Run against
v0.5.23 (HEAD 4e09172 in a scratch worktree, same test file): ambiguous from 18:34 to 20:04
and 20:29 to 21:14, no window ever accepted. Without the running hold (direction-aware
margin only) the replay still went ambiguous at 19:49 (gap 1.43). Store: the saved `state` keys equal the
dataclass fields, the new key round-trips, a v0.5.23 store loads. Attributes: a nested
`action_sources` change in place is written (old and new states differ), and the learning
sensor returns fresh dicts. `test_command_effort.py`'s table covers the half-size, capped
margin, the v0.5.23 parameters, signed drift with and against heat and cool (against still
widens the band; off keeps the margin) and the running hold (held only with the house
moving in the commanded direction; flat, unknown or wrong-way trend ends it; clear on
ignores the trend). Review fixes: the review repro (satisfied at 20:20, idle fetch 21:30)
is held at 19:54 (trend 0.077), ambiguous from 20:34 with no effort 20:39–21:29, and off
(`fresh report idle`) at 21:34; a fresh idle fetch at 19:55 under a held label counts one
contradiction (rate 1.0) and labels off, with either update order (timestamp sensor first
or climate entity first); a fresh running report 60 s before the command settles nothing,
60 s after it labels on. The live replay asserts the hold only at 19:49–20:04.
Report ordering: the live evening with the 18:07 and 20:07 fetches in either order
(timestamp sensor first, climate entity first) has no ambiguous label 18:11–21:15 and
accepted windows at every tick 19:30–21:14; with the hold disabled
(`report_settle_minutes` −1) the entity-first order breaks it (asserted), and the tick
after the entity-first fetch estimates 24.0 ± 0.1 (re-keyed mark). `tests/
test_attribute_size.py`: the 8-room × 2-system × 4-source house with full-precision
rows and 3 saved models per row stays under 12 KB for all three entities; exposed row
fields and saved-model fields are exact; the model's diagnostics keep the rest.

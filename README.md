# House Climate

A Home Assistant custom integration that makes several HVAC systems act as one house thermostat. Version 0.5.2 targets Home Assistant 2026.10 and Python 3.14.2+, with temperatures in °C.

The setpoint is the weighted **house average**. Each pass jointly chooses how much every available system should run to bring that average to the setpoint and reduce each room's deviation from it. A 45-minute prediction uses the learned room responses, outdoor losses, extra heat sources, solar gain and drift. There are no configured room mappings, individual room comfort rules or overshoot ceilings.

Each target is relative to that system's **own temperature sensor**: full heat requests its reading +1 °C and zero heat its reading −1 °C, mirrored for cooling. A warm living-room sensor therefore cannot silently disable the system best able to heat colder rooms. Intermediate efforts modulate the target; observed runtime and bounded rounding feedback help cycling thermostats deliver the requested effort. Device limits, target steps, effort hysteresis, dwell, shared mode selection and outdoor vetoes remain. Only changed commands are sent, including required hold presets. `last_decision` names which systems run or stay low and the rooms driving that choice.

## Install

In **HACS → Custom repositories**, add `https://github.com/Fledgewing/house-climate` with type **Integration**. Download **House Climate**, restart Home Assistant, then go to **Settings → Devices & services → Add integration** and select **House Climate**.

Alternatively, manually copy `custom_components/house_climate` from this repository into `/config/custom_components/house_climate` on your Home Assistant host, restart Home Assistant, then add **House Climate** from **Settings → Devices & services → Add integration**.

## Configure

1. Select the room temperature sensors and climate entities to control. Any number of systems is supported. No entity IDs are preselected; temperature sensors are filtered by device class and HVAC systems by domain.
2. Give each sensor a positive weight and, optionally, a zone label. Zone labels are descriptive; weights determine the mean.
3. Set a required hold preset such as `manual` for each system if your device needs one. Room effects are learned automatically.
4. Select outdoor temperature or fallback weather for learning, plus optional solar power and occupancy sources, then review the tunables. Temperatures must use °C; solar sensors in W, kW or MW are converted to kW.

Use the integration's **Configure** options to add or remove sensors and systems, change weights or required presets, and edit tunables. Added sensors count in the house mean immediately, start with neutral learning priors, and learn independently; removed sensors lose their model rows. Removing a system stops controlling it and leaves its last device settings in place.

Defaults: 5-minute control interval, 45-minute prediction horizon, 1 °C own-sensor run margin, effort hysteresis 0.05 and 5-minute effort on/off dwell. The mean-error integral gain is 0.001 per interval, with anti-windup and a ±1 °C bias limit. The joint objective and fan learner share `fan_reward_lambda` (default 1), balancing weighted room variance against weighted mean error. Legacy PI gains, house offset clamps and slew settings are retained for compatibility and are unused by the new allocator; the new mean integral has its own setting so upgrades use its small default.

Automatic mode retains its 1 °C deadband and 60-minute heat/cool dwell. Cooling is vetoed below 10 °C outdoors; heating above 24 °C. Explicit heat/cool requests take effect immediately, subject to the outdoor veto. Effort on/off dwell prevents chatter while a scheduled pass can turn a system on or off without waiting for house-relative offsets to slew.

## Room priority modes

`select.house_priority_mode` selects a named set of room weights. The initial **balance**
mode preserves your existing weights on upgrade. For example, create **night**, **day**
and **guests** to reduce guest-room priority overnight and give those rooms equal priority
when occupied. Weights are positive relative priorities; 1 and 3 mean a room contributes
one third as much as the other room.

In **Configure**, use **Priority mode action** to add, edit, rename or delete a mode.
**configure** follows the usual sensor/system wizard and edits the active mode's weights;
**edit** lets you choose another mode. Add/edit saves after the last sensor's weights;
rename/delete saves immediately. The last mode cannot be deleted. New modes start with weights of 1; newly added sensors get
weight 1 in every mode unless you edit it, and removing a sensor removes it from every mode.
If the active mode is deleted, the first remaining mode becomes active.

Selecting a mode runs control immediately and persists across restarts. It updates the house
mean, joint thermal allocation, fan rewards and the `minor_weight_share` health threshold. Spread
remains the range of eligible room temperatures. Mode changes preserve thermal learning and
fan room statistics; fan preferences are recomputed under the selected weights.
`climate.house` attributes `priority_mode` and `priority_weights` show the current priorities,
and `last_decision` names the mode. The existing `active_mode` attribute still means heat/cool.

There is no built-in scheduler. Use ordinary Home Assistant automations calling
`select.select_option`. After creating modes named **day** and **night**, for example:

```yaml
alias: House room priorities by time
triggers:
  - trigger: time
    at: "07:00:00"
    id: day
  - trigger: time
    at: "22:00:00"
    id: night
actions:
  - action: select.select_option
    target:
      entity_id: select.house_priority_mode
    data:
      option: "{{ trigger.id }}"
```

A manual override is simply selecting a mode (for example **guests**) in the UI or with the
same action. It remains selected until another selection, including the next scheduled one.
Disable or condition the automation if you want the override to outlast the next schedule.

## Warmer / colder

Set the mode on `climate.house` to **heat**, **cool**, or **heat_cool** (automatic heat/cool selection). Its target starts at 21 °C and is restored after restart. The thermostat card's +/− buttons adjust the house target in 0.5 °C steps.

Press `button.house_warmer` or `button.house_colder` to change the same target by ±0.5 °C. A target or mode change runs joint allocation immediately; repeated presses retain effort dwell and do not accumulate integral or rounding corrections. **Off** turns all available systems off.

`switch.house_climate_control` enables control by default. Turning it off observes and records without sending any commands, including off, fan and vane commands. Turning it back on immediately re-applies the house settings. The switch state survives restarts.

`sensor.house_temperature`, `sensor.house_temperature_spread`, and one `sensor.house_<system>_offset` per system explain the current readings and adjustments. `climate.house` exposes `last_decision`, the readings used, and the selected mode. Entity IDs shown here are the defaults generated by Home Assistant; renamed or conflicting IDs may differ.

Version 0.2.1 detects frozen sensors: a numeric value unchanged for `frozen_after` hours
(default **4**, tunable in options) is marked **not reporting** when systems have actually
heated or cooled for at least one hour of that window. Idle systems alone never trigger this.
Value-change clocks and observed activity persist in the Store; recorder history seeds startup
when available, so HA restarts and repeated identical reports cannot make old values fresh.
A sensor automatically rejoins on its first value change. If its registered device also exposes
a timestamp entity ending in `_last_seen`, an age beyond `stale_after` independently excludes
it until that timestamp becomes fresh. No device protocol or fixed entity IDs are required.

Not-reporting sensors are excluded from house temperature, balancing, exploration confidence,
and learning; their learning rows remain unchanged. Home Assistant Repairs names each affected
sensor and its last real change and asks you to check its battery. `climate.house` exposes
`not_reporting` details and includes them in `last_decision`; repairs clear on recovery.

Changes made directly on physical systems are noted in `last_decision` and re-asserted at the next interval. Sensors older than the configurable stale limit (default 720 minutes / 12 hours), unknown, unavailable, or nonnumeric are excluded. With no usable house sensors, targets fall back to the house setpoint with zero offsets; a system with no positive learned reach gets zero allocated effort. If a device lacks its own sensor reading, its target uses the house mean as a proxy and the decision explains this fallback. An unavailable system or failed service call does not block the others.

## Online learning and visibility

Each room learns `ΔT = a × (outdoor − room) + Σ b × signed_runtime_fraction + Σ e × source_on_fraction + c × solar_kW + d` using recursive least squares. HVAC action changes are tracked between passes: heating contributes positive running effort, cooling negative. Every `b` starts at 0.03 °C per interval, with zero prior heat-loss, sun-gain and drift means and broad covariance. Default forgetting is 0.998 per sample. Each room/system confidence is `n/(n+24)`, where n is the forgetting-weighted number of intervals with nonzero effort. In v0.4.2, low-confidence pairs blend toward that system's confidence-weighted mean learned b across rooms with runtime evidence, falling back to 0.03 only when none has been explored. A new sensor therefore inherits measured system reach while its own effect is learned; no room mappings are assumed. Forgetting and prior sample count are available in options.

While a system's least-confident active, weighted room is below `explore_threshold` (default 0.8), it gets a ±0.5 °C two-hour square-wave exploration offset, with different phases per system. The amplitude fades to zero at that threshold and perturbs bounded effort. `last_decision` says **exploring** and explains room-level allocation tradeoffs.

If a system has unexplored active rooms but has not actually heated/cooled for
`excite_after_minutes` (default **120**), v0.4.2 commands a learning run when the weighted
house mean still needs heat/cooling. Only one system explores this way at a time. Its target
is its own current temperature **+1 °C in heat / −1 °C in cool**, rounded and bounded by device
limits, using `run_margin` for the own-sensor margin. A warm
local sensor therefore cannot prevent learning about cold rooms. It ends after `excite_minutes` (default
**30**) of reported running action or **60** elapsed minutes, then normal joint allocation resumes.
Control off, mode changes, unavailable systems or reaching/passing the house setpoint cancel
the run. A system waits another inactivity interval before retrying. Restart retains inactivity
clocks but cancels an active run without counting downtime. Options expose both durations and
the confidence threshold; threshold zero disables thermal exploration. `last_decision` explains
“exploring: running <name> to learn which rooms it reaches”.

Read `sensor.house_learning` for overall confidence percent (the mean across room/system pairs). Under its attributes:

- `systems` → system entity → room sensor: `b` is the learned effect in °C per full-running interval; `blended_b` includes the prior; `influence` is the nonnegative normalized control weight; `confidence` is percent; `effective_samples` is the activity count.
- `sensors` → room sensor: `a` is heat loss, `c` sun gain in °C/interval/kW, `d` background drift, `fit_error` is the forgetting-weighted RMS prediction error in °C, and `samples` counts valid updates.

Press `button.house_reset_learning` to clear the model and confidence back to neutral priors. It retains the house target, mode and control state and starts learning again. Adding a sensor can restart exploration while that room learns. Confidence measures runtime evidence; learning needs distinct system operation to identify separate effects.

Learning requires outdoor temperature, known HVAC actions, and both temperature endpoints. Missing/stale room readings skip that row; missing outdoor context or a missing system action skips the interval. With no solar source selected its input is zero; a configured missing solar reading or unsupported unit skips the interval. Control continues with the current learned/prior blend during gaps.

## Extra heat, disturbances and pausing learning (0.5.1)

In **Configure**, select optional **Extra heat source entities**, then choose **binary**
(on/off states) or **power** for each. Power inputs use an **on above W** threshold,
default **10 W**; W, kW and MW are supported. These entities are only observed. For
example, a blower switch can indicate an insert running, or a plug power sensor can
indicate a portable heater. Choose an indication that reliably tracks the heat source.
Adding, removing or reordering sources preserves the rest of the thermal model.

Each source adds a learned effect `e` for every room, starting at zero with broad
covariance. Its input is the fraction of the interval it was on, including state changes
between control passes. `sensor.house_learning` exposes `sources → source entity → room`
with `e` (°C per full-on interval), confidence percent and effective samples. Configured
sources are part of the thermal prediction: their operation does not itself discard
thermal samples or fan experiments. Missing source observations skip the thermal interval.
Control continues responding to measured room temperatures.

Unconfigured heat or cooling is detected separately for each room after its mean HVAC
runtime confidence reaches **50%**. A residual outside
`max(0.3 °C, disturbance_sigma × fit_error)` per configured interval for
`disturbance_intervals` consecutive intervals in the same direction marks the room
**disturbed**. Defaults are **3 sigma**, **2 intervals** to flag, and
**3 within-bound intervals** to clear. A newly added source gets time to learn before
its active intervals can flag rooms. Other rooms keep learning; disturbed temperatures
remain in control. Only disturbed members with weight share at least
`minor_weight_share` (default **5%**) invalidate fan experiments; minor disturbed members
are excluded from rewards. `climate.house.disturbed` identifies the rooms and direction,
and `last_decision` says, for example, “unexplained heat in Guest Bedroom; not learning
from it”. Every RLS innovation is clipped to the same bound, including before confidence
reaches 50%, and clipped residuals update fit error so outliers cannot inflate the bound.
Set `disturbance_sigma` to **0** to disable detection and clipping.

`switch.house_learning` defaults to **on**. Turn it off to freeze thermal updates and
fan learning while temperature control continues using the current model. Set
`learning_pause_hours` to **0** (default) to pause until you turn it back on, or choose a
positive duration to auto-resume on the first control pass after the deadline. The switch
and absolute resume deadline survive restarts, including a deadline that expires while
HA is stopped. Resuming starts a fresh interval and fan hold; no paused time is learned.
The switch's `resume_at` and the thermostat's `learning_enabled` / `learning_resume_at`
attributes expose the status.

Inactivity clocks for thermal excitation persist in Store. Unknown clocks are seeded
from retained recorder heating/cooling history before control initializes them; an idle
restart therefore does not erase an earlier run. With no Store or recorder evidence,
observation begins at startup. Downtime never counts as observed runtime.

The seeded ten-day source simulation heats two of three rooms on nine random evenings
(295 full-on five-minute intervals). Mean absolute HVAC coefficient error is
**0.000144 °C/interval** with the source modeled, **0.005379** with it unconfigured and
disturbance protection enabled, versus **0.227647** with protection disabled. Learned
source effects are **0.85005 / 0.69999 / −0.00010**, against truth **0.85 / 0.70 / 0**.
Protection flags each heated room for **286/295** source-on intervals and never flags the
unaffected room; the modeled-source run has no flags. These are deterministic simulated
results, not measured house performance.

## Fan and vane learning (0.3.0)

House Climate also learns which fan and vane/swing settings reduce weighted room temperature
variance while keeping the weighted house mean on your setpoint. It reads each climate entity's
own `fan_modes` and `swing_modes` at runtime. Systems without either list have no corresponding
action. In v0.3.2, `learn_exclude_modes` in Configure is comma-separated text, default **auto**,
matched case-insensitively with whitespace trimmed. It excludes modes from both fan and swing
learning: auto lets the unit choose its own setting, mixing rewards the bandit cannot attribute.
Other advertised modes, including `swing`, remain learnable. An action with every mode excluded
is left alone. A system on an excluded mode moves to a learnable arm at the next scheduler
decision, using that window's one change. Excluded saved arms and choices are dropped on load.
Every learnable option starts equally; no speed or vane position is assumed to be better.

A contextual UCB bandit learns separately for each system and action type, heat/cool mode,
5 °C outdoor band, and running/idle status. It explores untried options, then balances measured
reward with further exploration. Each arm stores per-room mean error versus setpoint and
mean squared error, weighted by observed seconds across clean hold windows. Under the current
normalized weights, reward is `−(Σ w·E[e²] − (Σ w·E[e])²) − λ·(Σ w·E[e])²`.
These statistics survive priority changes and rewards are recomputed before choosing an arm.
There is no fan-statistic forgetting (as in v0.3.2). Legacy scalar-only arms load with empty
room statistics and zero samples so they can relearn; thermal learning is retained.
`fan_reward_lambda` defaults to **1**. `fan_hold_minutes` defaults to **30 minutes**; a single
global schedule holds each experiment before measuring and selecting the next action. Only one
action of one system changes at a time. Required hold presets continue to be re-asserted.

Windows containing sensor transients, missing/stale/not-reporting members, mode switches or user
setpoint changes are discarded, including events between control passes. Changes of the selected
context also discard a window. Outdoor context is required. The learner and selected settings
persist; restarts begin a fresh full hold without treating downtime as evidence.

`switch.house_fan_control` defaults to **on**. Turn it off to leave fan and vane settings alone
while temperature control continues. `switch.house_climate_control` off suppresses all commands.
Both switch states persist. Outside changes to selected fan/vane settings are re-asserted through
the same global schedule; outside-change notices use the existing two-minute echo grace period.

`sensor.house_fan_learning` shows evidence confidence as a percentage. Attributes under
`systems → system entity → fan/swing → context` show the current sampled `best` setting and
`options` with each option's current `mean_reward`, `samples` and `rooms` error statistics. Context keys are `heat|0|1`, for example:
heat, 0–5 °C outdoors, running. Confidence averages `n/(n+10)` across contexts, with `n` the least
sampled option's count; it measures coverage, not statistical certainty. `last_decision` on both
this sensor and the house thermostat explains exploration, the best setting so far, or re-assertion.
The reset-learning button resets fan/vane learning too.

Quiet hours are optional and **OFF by default**: no fan limits apply unless you enable
`quiet_hours_enabled` in Configure. `quiet_start` defaults to **22:00**, `quiet_end` to **07:00**,
using Home Assistant local time and supporting midnight crossings (end exclusive; equal times
mean no quiet window). Each system's optional `quiet_max_fan` selector comes from its advertised
fan modes. During quiet hours, options after that maximum in list order are excluded, with no
exception for `auto`. Outside quiet hours all learnable modes are eligible. With no maximum
selected there is no quiet cap. A quiet cap takes effect on the first pass inside the window,
interrupting the current experiment and discarding its sample, with at most one action changed
per pass; vanes have no quiet restriction.

## State and recording

The integration persists the target, mode, integrators, offsets, and last mode-switch time in Home Assistant's `Store`, with `RestoreEntity` as a thermostat-state fallback. Each interval records every member sensor (including unavailable/stale readings), outdoor temperature, solar power, occupancy, and each system's observed mode, target, offset, and HVAC action. Observations are taken before commands. Records older than 30 days are pruned on each control run. Data lives in `.storage/house_climate.<entry_id>` under the HA config directory.

The Store also persists model coefficients, covariance, confidence counts and fit errors. Restarting retains learning and begins a fresh runtime interval rather than guessing what happened while HA was stopped. Config-entry version 3 stores named priority modes in entry options. Upgrades preserve existing sensor weights in balance, remove obsolete v1 room mappings, and retain other settings and control state. The active priority mode is stored alongside control state.

## Development

```sh
uv sync --group dev
.venv/bin/pytest -q tests
uv run ruff check custom_components tests
uv run ruff format custom_components tests
```

The dev group pins `pytest-homeassistant-custom-component==0.13.375`, which targets HA 2026.10.4. The controller, thermal learner and `fans.py` have no Home Assistant imports. Tests include a seeded two-day, six-room simulation with the real controller, nonuniform system effects, day/night weather, solar and cycling thermostats. Every learned b must be within 0.003 °C/interval of truth; the house mean must stay within 0.5 °C throughout day two, with lower final spread than learning disabled and exploration ended.

Fan tests include a separate seeded physical-house simulation with three anonymous fan options,
5-minute physical steps and 600 half-hour experiments. The bandit must discover the setting that
reduces equilibrium spread from 4.0 °C to 0.6 °C, select it in over 90% of the final 100 windows,
and keep the mean within 0.05 °C of target. HA tests verify commands, hold timing, switches,
outside changes, skipped windows, options and Store reloads.


The shared-sensor regression starts living at 24.5 °C, downstairs at 19.4 °C and guest at 18.7 °C, with both systems sensing living. A strongly heats living; B's stronger cold-room reach starts with low confidence. On pass one, B is commanded **0.5 °C above its own reading**, A **1 °C below**. At 12 simulated hours the weighted mean is **22.6295 °C** (target 22.5) and weighted room standard deviation **0.4381 °C**.

The original seeded two-day learning simulation's final max−min spread improves from **0.152566 °C** in v0.5.1 to **0.151194 °C**. Day-two mean stays **20.9450–21.0796 °C**, targeting 21 °C; coefficient accuracy and exploration checks remain. With B unavailable in the shared-sensor house, A alone cannot maintain balance: the 12-hour mean is **15.2539 °C**, weighted standard deviation **7.2731 °C**. Grid searches verify A's objective-minimizing effort; a seven-day replay verifies bounded spread (peak **8.3697 °C**, last-day range **0.1316 °C**). The objective permits this tradeoff when the remaining system has poor reach. These are deterministic simulated results, not measured house performance.

The late-sensor regression also retains a frozen v0.4.1 controller comparison: B starts immediately under v0.5.2; the historical controller leaves it idle. Final weighted room standard deviation is **0.8438 °C** versus **2.4516 °C**. See `DESIGN.md` for fixture dynamics and explanations of existing test updates.

## Lasting room changes

Each room learns a fast challenger alongside its protected active model. A lasting
change can become the new normal after the challenger predicts at least 30% better
in 80% of the two-hour blocks over 24 hours, with evidence across day/night and every
system running/idle. Brief heater use and recurring evening heat do not qualify.
The previous model is saved, including all its learning; up to three saved models
are kept per room, evicting the one that has gone longest without fitting. Returning
to a proven saved model needs only four hours of evidence. All models and evidence
survive restarts.

Each room sensor creates controls named after that sensor:

- **Accept new normal**: use the challenger immediately and save the previous model.
- **Relearn room**: restart that room's active learning, keeping saved models.
- **Temporary change**: hold the current model and pause room learning and automatic
  model changes. Turn it off manually, or configure `temporary_hours` for auto-off
  (default 0 means manual). Ordinary temperature control continues.

Options expose `regime_margin` (default 0.30), `regime_hours` (24),
`regime_return_hours` (4), and `temporary_hours` (0). Windows use complete two-hour
blocks; missing data or insufficient runtime/day-night variety can delay a switch.
A warning repair appears after 12 hours of continuous disturbance, suggesting
**Accept new normal** if the room has changed. It clears on a switch or recovery.

`sensor.house_learning` includes each room's active model, saved models with last-fit
timestamps, and challenger advantage in percent. Positive advantage means the
challenger predicts better. Removing a room sensor removes its controls and models.

In the seeded simulations, a permanent window change and a week of portable AC
were accepted after 24 h 10 min. Removing the AC restored the unchanged original
model after 4 h; bringing the AC back a week later reused its saved model after 4 h.
A two-hour heater and nightly unconfigured stove caused no switches.

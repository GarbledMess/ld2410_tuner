# LD2410 Tuner

Home Assistant custom integration for labelled radar calibration and gate history.

## Installation

### Manual installation (private repository)

Copy only the repository's `custom_components/ld2410_tuner` directory into
`<Home Assistant config>/custom_components/ld2410_tuner`, then restart Home Assistant.
Add **LD2410 Tuner** under **Settings → Devices & services → Add integration** and
refresh the browser fully to load the panel. Do not copy the repository root into
that folder. Existing stored training/history are preserved; earlier recommendations
must be learned again before Apply. Local brand icons are supported by Home Assistant
2026.3+.

### HACS custom repository (requires a public GitHub repository)

This project uses the HACS-compatible folder layout but is not being submitted to
the HACS default catalogue. [HACS cannot install private repositories](https://www.hacs.dev/docs/faq/private_repositories/),
so use manual installation while this repository remains private.

If you later choose to make the GitHub repository public, it can be added manually
in HACS under **Custom repositories** using
`https://github.com/GarbledMess/ld2410_tuner` with type **Integration**. Download
**LD2410 Tuner**, restart Home Assistant, and add the integration as above.
Adding a custom repository does not require submitting it to the default catalogue.

## Rooms and independent zones

The **Rooms and zones** card assesses multiple radars together using their current
thresholds. Home Assistant areas supply default groups containing the area's
recording-enabled radars. **Edit members** overrides that selection; **Restore
area default** restores it. **Add zone / manual group** creates an independently
assessed selection, optionally organized under an area. These settings do not
change Home Assistant's registry or require the optional ESPHome package.

For example, **Office** can contain **Desk** and **Sofa** zones, each with its own
radar selection. The Office assessment can cover both, while the two zone
assessments stay separate. A person at the desk does not count as sofa occupancy,
and a desk detection cannot hide a sofa miss. A radar selected in multiple zones
contributes to each; grouping cannot locate a person within that radar's coverage.
Area membership for manual zones is organizational: the whole-area default still
uses the actual device areas, unless you explicitly edit its members.

Each member's existing labels are interpreted as occupancy within its coverage:
any Present label establishes group occupancy; Empty requires every member to be
labelled empty. Human labels override estimates on that same member. An explicit
Unknown suppresses automatic fallback. No labels are propagated from a whole area
into its zones. Review old labels that described a wider area before using them
for a narrower zone. Automatic evidence remains confidence-weighted and is clearly
identified as an estimate, not independent validation of the detector.

**Assess current settings** replays 24 hours, 3 days or 7 days of recordings.
Each radar's own timeout and configured delay policy are replayed first; detections
are then combined. Any member may detect presence, and any member may cause a
false positive. Overlapping detections count once. Only shared recording coverage
is assessed; missing readings, gaps and timing warm-up are excluded and reported.
A history window limits the assessed dates, without deleting recordings.

The result includes the combined score, occupied/empty time, hits, misses, false
triggers, correct empties, and each radar's exclusive occupied-time contribution.
All valid readings are included, without the individual learner's outlier filter.
The replay follows continuous recordings across label transitions so existing
holds can extend into an empty period. It therefore differs from per-device Learn
and its stored scores; the per-member rows instead use identical group evidence.
Observation counts describe estimated detection at unique recording timestamps;
elapsed time determines the score, with the existing 5:1 missed/false-active cost.
Timing and unknown-label limitations remain visible. A high score on these
recordings is not a guarantee of future accuracy.

Assessment runs on the server and can finish after leaving the panel. The latest
result per group is stored with its scorer version. Polling and new recordings do
not recalculate it. Changed membership, labels, thresholds, timing or scorer
version mark it for reassessment; use Assess again to refresh the evidence. An
integration restart cancels unfinished jobs, which can be requested again.

### Joint learning within a zone

**Learn together** fits all selected radars against the last seven days of shared
recordings. Device Learn and overnight learning use the most specific unambiguous
multi-radar zone, or the area when it has no independent zones. Single-radar zones
keep individual learning. Overlapping manual groups require selecting the intended
group explicitly; a whole area containing independent zones cannot be learned as
one zone. Overnight learning handles each joint group once and shares its outcome
with all members.

A miss counts only when no member detects presence after its own timing filters.
Every member's false activity counts against the group. Strong overlapping
signals are retained: equally scoring settings favour thresholds inside supported
noise/presence gaps across all radars. There is no strongest-radar winner or fixed
threshold floor. A weak, nonseparating radar is not forced to detect every occupied
moment. The report shows combined outcomes without double-counting occupied time.

The joint learner uses the same continuous interval replay as group assessment,
including valid spikes and lower-confidence automatic labels. It uses a bounded,
deterministic coordinate search with multiple starting points; it does not claim
the globally optimal solution or independently validated field accuracy. Joint
measurements are not interchangeable with individually filtered learner scores.

Results appear in each member's existing User learnt / Autolearnt slot, with joint
outcomes clearly identified. Apply on a device opens the group review. The group
shows every proposed threshold and applies the complete recommendation. Automatic
Apply requires every member to permit the learning source and a lower joint time
penalty on the same fresh evidence. A disabled member blocks automatic application
for the whole group.

Membership, labels, active gates, timing, current thresholds and saved results are
checked before and during writes. Lower thresholds establish replacement coverage
before higher thresholds withdraw old coverage. Writes use the existing paced
writer and reported-value checks; they are not atomic hardware transactions.
Failure stops further writes and leaves any already-written values in place.
Current/Previous rotate only when all members finish successfully. A joint result
whose companion configuration has changed must be relearned; it cannot be applied
as a standalone device pattern. Jobs survive leaving the panel and report
interruption on integration restart. No new live presence entity is introduced.

## Learning in the background

**Learn thresholds** starts a server-owned job. You can switch pages, close the tab,
or reconnect without losing the calculation or its result. Each device shows its
latest job above the collapsible sections: checking settings, fitting/validating,
then completion or the reason it could not finish. The progress bar is
indeterminate; elapsed time is shown without claiming a percentage complete.

Returning to the panel retrieves the job status. **Review result** opens the saved
recommendation. Manual Learn saves a preview by default; the global
**Every completed Learn** option also enables automatic Apply after manual learning. Repeated requests for one
device share a calculation. Manual and overnight callers use the same job lifecycle,
with their results saved in their respective slots. The latest job report is bounded
to one per device; automatic Apply adds its bounded comparison and write outcome.

Home Assistant shutdown/restart interrupts an active calculation. The report marks
that interruption so you can retry with Learn; unfinished calculations are not
resumed automatically. Previously completed results remain saved.

## Overnight learning and saved results

Enable **Overnight learning** at the top of the existing panel, choose a time and
save the schedule. It starts disabled with a suggested time of 03:00 and uses Home
Assistant's configured timezone. Each enabled daily pass learns the devices one at
a time. In global learning settings, **Automatically apply better results** has an
off switch and an **Apply after** selector:

- **Overnight learning only** (default): manual Learn saves a preview.
- **Every completed Learn**: manual Learn can also apply improvements, even if
  the overnight schedule is disabled.

These settings are global defaults. In each sensor's **Recommendations**, choose
**Automatic Apply for this sensor**:

- **Inherit global** (default): follows the current global enable/scope settings.
- **Off**: keeps this sensor's learns as previews.
- **Overnight learning only**: allows scheduled improvements for this sensor.
- **Every completed Learn**: also allows its manual learns to apply improvements.

An explicit sensor override takes precedence even when the global default is off.
Changing the global defaults affects inheriting sensors; existing overrides remain.
Selecting **Inherit global** removes the override. The effective setting appears
below the selector, saves immediately, and survives restarts and clearing recordings.
The overnight schedule remains global: an override does not enable a disabled
schedule, bypass paused recording, or bypass the improvement and write guards.
Existing installations inherit their current global settings without migration.
If Home Assistant was offline at the scheduled time,
the pass runs after startup. A persisted daily marker avoids duplicate runs after
restarts or during the repeated autumn clock-change hour. A skipped springtime
clock hour runs at the first check after the jump. Disabling the schedule lets an
in-progress device finish and stops before the next device.

After a fit allowed by the sensor’s effective setting, automatic Apply compares the saved recommendation with the
**live thresholds**, on one fresh snapshot of the same evidence and timing policy.
It applies only a strictly lower unrounded weighted error cost: each 1% of missed
occupied time costs five points, and each 1% of false-active empty time costs one.
Equal or worse results and insufficient evidence cause no writes. This keeps the
existing balance between missed presence and false activity; it does not require
perfect training results or replace the radar's presence handling.

Stored display scores are not reused to authorize writes: those scores may have
been calculated on different recordings. The fresh comparison is saved in the
latest learning-job or overnight report with its scorer version, sample outcomes
and timings. Shared manual/overnight jobs reuse one application decision.
This is improvement on recorded evidence, not proof of improved live accuracy.
Human labels retain priority, while automatic labels remain lower-confidence evidence.

The report shows **applied**, **skipped** (with its reason), or **incomplete/failed**.
Successful application moves the replaced settings to **Previous**. The normal
paced writer and reported-state checks are reused. Changes to labels, timing,
configuration, recording, or the auto-apply setting stop further writes; already
written gates cannot be undone atomically. A partial write is never marked fully
applied. Setting a sensor’s override to **Off** stops its further automatic writes.
Narrowing its effective scope to overnight only stops further automatic writes
from a manual job. Global default changes affect inheriting sensors in the same way.
Turning off the schedule prevents scheduled automatic application; manual jobs
remain eligible when the sensor’s effective scope is **Every completed Learn**. Selecting saved
patterns and checking stored scores never auto-apply.

Each device header shows its latest overnight outcome, including insufficient data,
failed or interrupted runs. Tap the outcome to open its dated report in
**Recommendations**, with the failure reason and a next step. Technical details
remain available without needing to hover. A failed device does not prevent other
devices learning. If labels change during a scheduled calculation, the job retries
once using a fresh snapshot; a second change is reported, not silently accepted.
A result can have **sampling uncertainty** even with all three package timing
values available: snapshots cannot locate every transition and short labelled
periods may contain no measurable duration. The Apply button shows estimated
recall and false-active time instead of the misleading "Timing uncertain" label.
Missing timing controls, configured fallback values and disabled timing remain
explicitly described in the timing section. No package update is needed for this
presentation change; saved results retain their original assessment.

The **Recommendations** selector reviews four bounded saved-result slots:

- **User learnt:** latest manually requested Learn result.
- **Autolearnt:** latest completed overnight Learn result; it does not replace User learnt.
- **Current:** last fully applied result, checked against Home Assistant's reported states.
- **Previous:** settings replaced by that successful Apply, available for rollback.
  On the first Apply, the original device thresholds are captured without inventing
  accuracy measurements. If settings were changed outside the tuner, those actual
  pre-Apply values become Previous instead of an outdated saved result.

Changing the selector updates the report, gate table, graph and Apply button together.
Only a fully successful Apply rotates Current/Previous. Quality warnings remain
advisory. Selecting a saved result explicitly allows restoring it after labels or
thresholds changed; the earlier accuracy report is identified as stale when labels
changed. Apply checks that the selected result and the currently displayed device
configuration are still the ones reviewed. Old-model or incomplete results and
incompatible/unavailable gate configurations still require a fresh Learn.

The four slots are stored with the existing integration data and survive restarts;
they contain thresholds and diagnostics, not duplicate energy recordings. Clear data
also removes them. No extra tabs or automatic threshold application are introduced.

## Device timing in learning

Learn reads the radar's exposed **Timeout**. It evaluates the combined gates first,
then estimates how the existing hold would affect missed presence and false triggers.
If both ESPHome presence delays are exposed, it also evaluates `delayed_on` followed
by `delayed_off`. The optional package publishes these as read-only diagnostics;
[standalone configurations are supported too](docs/esphome-recovery.md#exposing-timing-for-calibration).
Apply still writes only gate thresholds; the effective global or sensor setting controls which learns
can automatically apply improvements.

Recommendations show the timing used, unknown settings, original raw counts and
sampled timing estimates. A saved result warns when current timing differs. Old
model results need a fresh Learn. Unknown timing is a caution, not a requirement
to install the package or a quality-based Apply block.

Recordings are snapshots, normally about six seconds apart. Consecutive high
observations are treated as a sampled run; hold starts from its last observed hit.
For empty-room errors, possible activity between neighboring observations is also
included, so a lone reading is not assumed to be a harmless sub-second spike.
An on-delay transition can start anywhere between the preceding low snapshot and
first high snapshot. The missed-presence range shows both onset assumptions, rather
than treating the recovered high reading as another definite miss. The finite error
cost uses the conservative end of that range; uncertainty is not an absolute
requirement to lower thresholds. Unresolved timing remains visible and marks
otherwise-passing results yellow rather than confirming field accuracy.
This range covers onset uncertainty within the sampled replay, not all unrecorded
activity. Replay restarts at label changes and gaps over twelve seconds. Initial observations
up to the larger of Timeout + off delay and on delay are left unscored because the
preceding state is unknown; their count is shown and the raw evidence is retained.
Too little remaining evidence cannot produce a passing result. Recent validation
keeps earlier timing context; the chronological backtest starts independently.

These estimates help choose gate thresholds; they do not reconstruct unrecorded
pulses, establish exact durations, or guarantee real-world accuracy. Review links
use the same timing assessment and show the span between observations separately
from actual duration. Longer timeouts cover quiet gaps but also extend false
presence. Delay/reporting options and their behavior changes are listed in the
[package guide](docs/esphome-recovery.md#timing-and-reporting-options-behavior-changes-to-consider).

## Repository layout

The installable package is `custom_components/ld2410_tuner`. Its entrypoint owns
Home Assistant setup/unload; separate modules own discovery, recording, manual
labels, automatic inference, threshold fitting, charts, and websocket commands.
Supporting Python modules are grouped into `runtime/`, `presence/`, `history/`,
`calibration/`, and `presentation/`. The panel entrypoint loads focused modules
from `static/panel/`.
See [architecture and invariants](docs/architecture.md) for the full ownership map.
Tests, developer dependencies, replay tools, and CI remain at the repository root.

Only the integration directory is installed by HACS. Repository metadata, tests
and CI stay outside it. The layout follows the
[HACS integration requirements](https://www.hacs.dev/docs/publish/integration/).

## Global learning timing

Open **Learning timing** in the **Global settings** card, alongside the overnight
schedule and recording storage controls. The
same policy applies to manual and overnight learning on every device:

- **Use available device timing** (default): missing durations stay unknown.
- **Use defaults where device timing is missing**: fill each missing/unreadable
  duration independently. Valid device values, including zero, always win.
  The editable defaults start at 1 second radar timeout, 0.5 second on delay,
  and 1 second off delay; they are unused until this mode is selected. Set them
  to match your firmware. Results identify fallback values as assumptions.
- **Disable timing adjustments**: score raw gate-threshold activity without
  timeout or on/off delays. This changes the learning model, not the device.

Saved results retain the timing used when fitted. Learn again after changing the
policy; Apply still writes only gate thresholds. Missing timing is not a job
failure. A completed overnight pill shows recorded recall and false-active time
when available; amber means caveats or uncertainty, while red identifies a job
error or poor measured results. Open the report for timing assumptions, estimated
ranges, and the distinction between human and automatically labelled evidence.

## ESPHome recovery

For missing settings after boot, Query Params / Radar Restart buttons, and Apply
that appears to do nothing, see the [ESPHome recovery guide](docs/esphome-recovery.md)
and [complete LD2410C package](examples/esphome/ld2410c.yaml). Keep each node's
existing UART pins and host configuration; the package supplies all radar entities
and includes recovery in one self-contained file. Include the same file once per
radar with a unique internal prefix, UART ID and Home Assistant subdevice; see the
[multi-radar example](docs/esphome-recovery.md#multiple-ld2410cs-on-one-esp).
Existing single-radar includes and entity names remain unchanged. The tuner now
checks every 10 minutes and requests Engineering Mode on only for radars with
recording enabled that report it off; see [automatic Engineering Mode](docs/esphome-recovery.md#automatic-engineering-mode).
The package is optional: the
tuner also supports existing ESPHome configurations with the required entities.
Recovery runs primarily on the ESP; the tuner checks readiness, paces writes and
retains failures.

## Calibration workflow

1. Expose the per-gate energy and threshold entities on the LD2410. The tuner
   enables Engineering Mode for recording-enabled devices when an unambiguous
   switch reports off; otherwise
   enable it in ESPHome. Entity IDs must retain the existing `*_g0_move_energy` /
   `*_g0_move_threshold` naming convention (gates 0–8, move/still).
2. Label the empty room NOT PRESENT with the normal fans, curtains and background
   activity. Use the timeout to avoid accidentally leaving a label running.
3. Label people PRESENT, including walking and sitting very still at each location
   that matters. Collect multiple sessions, not just a short walk past the sensor.
4. Select UNKNOWN between sessions to let automatic estimates contribute, or
   correct recorded periods using the chart. Human labels override guesses. A
   chart range explicitly marked UNKNOWN is excluded from both training sources.
5. Click **Learn thresholds**. Review device-wide validation in **Recommendations**,
   then **Apply learned thresholds** when you choose to use the candidate. Test again in the
   actual room, especially quiet sitting and empty-room false triggers.

The learner evaluates all enabled gates together: presence succeeds when any gate
triggers; an empty-room trigger from any gate is a device false positive. Per-gate
misses are not device misses. A threshold of 100 disables a noisy or redundant
gate and can be valid when other gates cover presence. If retained empty and
presence signals overlap so that no threshold combination cleanly separates them,
the result is marked red and the conflicting periods are available for graph review.
You can still apply those learned thresholds explicitly.

The learner needs at least 50 usable timed observations of each state, combining
human labels and confident automatic estimates. There is no minimum human percentage
and no human-only sample quota for Apply. It retains up to 5,000 recent observations
per class and source for fitting. Missing gate readings remain missing; usable
observations from other gates still contribute. Gates with no observations keep
their current threshold.

History is sampled at a minimum five-second spacing on a two-second timer (normally
six seconds), independently of whether values change. Existing gate histograms can
include legacy data without timestamps; the learning result reports the timed
human and inferred observations actually used, separately from gate histogram counts.

The Apply button is red when combined targets fail, yellow for unknown timing, limited human
measurements or backtest concerns, and green when measured results meet the targets.
Its text also states the outcome. It is disabled when no learned thresholds exist
(and temporarily while an operation is running). The confirmation remains explicit;
quality warnings alone neither authorize automatic writes nor prevent a manual Apply. Invalid
thresholds, old models and incompatible/unavailable device configuration still
require a fresh Learn preview. Explicitly selected saved results can be restored
after label changes; their older measurements are identified in the panel.

The Recommendations section contains Learn/Apply and one device-wide report;
expand its gate table for individual thresholds and sample counts. The chart shows
the selected gate's results. Exports and Clear data are grouped under Data & export.
Actions display a named spinner and lock conflicting controls on that device until
they finish. Slow background refreshes show a separate loading indicator.

In **Past presence labels**, choose a day and drag across the time bar to fill the
From/To fields. Opening this section brings the energy graph into the editor.
Choosing a day loads that day's readings; choosing a saved period zooms around it.
Graph selections, timeline handles and time fields share the same selected period.
Refresh keeps the historical window fixed. Conflict-period buttons in Recommendations
open the corresponding readings for review.
Drag either handle to adjust the range, or use the time fields.
Focused handles also support left/right arrows (one minute; Shift for 15 minutes).
Choose the status and save to confirm the change; dragging alone never saves a label.
Selecting a saved period keeps the existing edit/remove workflow.

## Charts

Changing the history window (for example 6h to 24h) keeps the same ending timestamp.
**Refresh** explicitly advances to the latest data. Chart history stays pinned
between refreshes while the live state and energy summaries continue polling.
Returning to a previously loaded window uses a bounded cache. Label changes
invalidate cached labels, and the graph remains visible during refresh failures.

The line is the bucket mean and the highlighted band is the sampled minimum–maximum.
The bucket duration is shown above the plot: wider windows use coarser aggregation,
so averages can look smoother while recorded peaks remain in the band. Buckets are
aligned to absolute time rather than shifted on every request. Gaps are not joined
with invented continuous lines; labels and selections use the same timestamps.

An unsafe chart separates the selected gate's own false-positive sample count from
the combined device failure reasons. The largest per-gate contributors are shown
in the chart and learning details. A device-wide rejection does not mean every gate
is noisy: detection uses any enabled gate, and overlapping per-gate counts cannot
be added. These counts describe sampled threshold crossings, not individual physical
occupancy events. Version 1.9.2 corrects this diagnostic attribution and updates the search while
retaining the existing acceptance criteria.

## What learning measures

The candidate models detection as any enabled gate energy **strictly above** its
threshold, following the [ESPHome LD2410 threshold contract](https://esphome.io/components/sensor/ld2410/#number).
The automatic estimator compares each gate against human-labelled occupied and
empty-room distributions using smoothed likelihoods. It combines nearby-gate
support and a two-state Bayesian temporal filter, accumulating persistent weak
signals while making exit slower than entry. Move/still at one gate count as
correlated evidence, not two independent votes. Entry confirmation requires
continuing signal support, so residual filter memory after a brief spike cannot
alone confirm presence. Evidence accumulates by elapsed time, with four seconds
of confirmation for presence and eight for absence. Gaps over ten seconds reset
temporal confidence. Missing channels cannot certify absence; strong directly
human-guided evidence on the available channels can support presence at no more
than 60% confidence.

Before human references exist, it warms up a background estimate and starts making
tentative guesses. Its background stops adapting during likely occupancy so a
stationary person is less likely to become the baseline. Bootstrap confidence is
capped at 65%; partially human-guided estimates at 80%; fully guided estimates at
98%. These scores are **heuristic**, not empirically calibrated probabilities.
A completely flat startup stays UNKNOWN until observed variation or a human
empty-room reference supplies more evidence. Empty-room p90 energy provides the
baseline for elevation, and contradictory gates reduce weak positive evidence.
A room already occupied during startup remains fundamentally ambiguous without
human references; this is not solved by assigning a higher confidence score.

The [autolabelling replay report](docs/autolabelling-validation.md) records gains,
regressions, and limits on the supplied recordings. Version 1.9.3 changes automatic
labels and code organization; Learn/Apply remain explicit and retain their fitting
targets. Quality is advisory for explicit Apply. No software presence entity is added. The effective global or sensor automatic Apply policy controls
whether manual or overnight runs can apply a measured improvement.

Each stored observation retains its automatic label and confidence alongside the
gate energies. Confidence weights the observed time represented by each estimate;
extra readings within a brief fluctuation do not multiply its influence. Automatic
scores refine candidates after human-labelled performance and supported separation.
Human priority is enforced by ranking, not by the relative volume of estimates.
The sample-weight totals shown in calculation details remain legacy diagnostics,
not the time-based objective. Scores below 55% are not used. Old history without
confidence is readable but gains no invented estimates. Human corrections and
explicit UNKNOWN ranges override stored guesses. Feedback buttons adjust estimator
bias; use chart/training labels to supply known occupancy examples.

Before fitting, a detector identifies rare low-energy clusters separated from the
normal presence distribution. A candidate cluster must overlap recorded noise and
be separated by at least three times its own interquartile spread (one energy unit
for a flat cluster). Exclusion additionally requires a brief dip of at most six
samples, two surrounding observations on each side, continuous timestamps, and no
independent above-noise presence support from another informative gate. It never
removes a complete presence episode, sustained quiet presence, or empty-room spikes.
The 1% upper bound limits exclusions; it does not trim a fixed percentage of data.

The same retained observations feed fitting, sample counts, accuracy, episode/run
checks and recommendation quality. Human and automatic outlier
counts are shown separately. Original recordings and labels remain unchanged; raw
measurements are available separately in the exported `raw_audit` result and do not
participate in acceptance. The detector does not use candidate thresholds or errors
to decide what to exclude.

The quality target remains **99.9% of observed occupied time**, but search uses a
continuous, finite error cost rather than treating that target as a free allowance
for missed presence or requiring perfect recall at any noise cost:

`error cost = 5 × missed occupied-time percentage + false-active empty-time percentage`

Lower is better. The 5× factor is an explicit preference for avoiding missed
presence, not a measured physical property. Each percentage uses its own observed
class duration, so recording much more empty than occupied time does not drown out
presence. Missed time uses the conservative end of the reported onset range.
Automatic evidence uses the same cost with confidence-weighted time, after the
human evidence. Episode coverage, recent-time cost and event counts break ties.

The separate displayed false-positive score is still **0 at best; −1 means 1% of
empty time falsely active**. There is no hard false-positive percentage or
frequency rejection limit. The 99.9% target remains visible in quality assessment;
a cheaper compromise that misses it is marked red, never presented as meeting it.
Gates are combined before timeout and known on/off filters are replayed. A gap
covered by those settings does not incur a missed-presence penalty; an on-delay
only rejects an activation if it remains too short after radar hold.

Only time within continuous recorded sessions counts; gaps and isolated readings
do not invent minutes of evidence. Isolated human observations still take priority
over automatic guesses, but have no measurable duration. Unknown timing uses
midpoint estimates between snapshots and is marked unknown. These snapshots cannot
prove sub-second pulse lengths. Existing outlier exclusions and human labels are
unchanged; no additional observations are removed to improve a score.

When both human-labelled classes establish separation, the preferred threshold
is halfway between the highest
recorded empty-room energy and the lower quartile of retained presence energies.
Human-supported separation takes priority over automatic preferences; actual
retained human detections take priority over that preferred margin. There is no
fixed minimum threshold. Useful overlapping gates remain enabled; suppression at
100 is still possible when a gate's observed background requires it.

The search also tries per-gate false-positive bounds and a quiet starting point
when the current candidate has a nonzero human error cost. A bounded pair search
can raise a noisy gate and lower a supporting gate together. Confidence-weighted
inferred observations refine the result after human performance and separation.
Without examples of both human-labelled states, the existing background-anchored
search is retained; automatic estimates alone do not enable the extra per-gate
penalty or margin preference.
Their proportion never rejects a recommendation; human labels and explicit UNKNOWN
ranges override stored estimates for the same period.

When enough human-labelled observations exist, the latest 20% of each class is used
for an earlier-data backtest. That evaluation excludes newer inferred observations
to avoid leaking holdout labels through the estimator. The outlier detector is also
trained on the earlier subset and frozen for the holdout; holdout exclusion counts
are reported separately. The final recommendation is
then refit using **all retained** eligible observations, including newer estimates. Its
training results and the earlier-data backtest are reported separately. A failed
backtest does not veto a final candidate that learned the newly observed pattern.

Recommendations are red when the occupied-time target is missed, an entire
measurable occupied session is missed, or the candidate remains active throughout
observed empty time. Otherwise false-active time is a scored tradeoff, not a failed
learning job. Timing uncertainty and insufficient evidence are shown separately.
All quality states remain advisory for explicit Apply. Automatic-only results are
labelled estimates and do not claim human-validated accuracy.

These are observed metrics, **not proof of near-perfect field accuracy**. Adjacent
samples are correlated; a backtest from the same session is not an independent room
trial. Firmware hold time, installation, unsampled spikes, range resolution, and
custom filtering can affect actual detection. Search is iterative and is not a
proof that no feasible configuration exists. The physical radar still receives
static per-gate thresholds, not the Bayesian estimator itself. Test quiet sitting,
different positions and empty-room sessions after applying a recommendation.

All active gate threshold entities must be exposed. Exposed maximum-distance gate
settings restrict the modeled gates; absent maximum-distance entities are assumed
to cover gates 0–8, so expose those settings when using a reduced detection range.
Per-gate energies require [engineering mode](https://esphome.io/components/sensor/ld2410/#switch).
Automatic analysis is a confidence-scored training source, not a replacement HA
occupancy entity.

Saved results retain the measurements and model used when they were learned; they
are not silently rescored. Older models are labelled explicitly and require a new
Learn before Apply. The chart line, chart summary, Apply button and confirmation
use the same quality assessment and current timing: red indicates poor results,
amber indicates concerns or uncertainty. Sample-based legacy diagnostics are
labelled as historical, not presented as current time-based limits. Automatic
recorded-time summaries are unweighted diagnostics; search additionally weights
automatic time by confidence after the human evidence.

The report also keeps snapshot outcomes visible: presence hits, missed presence,
correctly empty readings and false presence. Timing-aware counts use the modeled
hold/delays and show ranges when transitions are unresolved; raw counts remain
identified as such. These diagnostics help judge a recording but do not replace
the time-based optimization cost or turn a completed learning run into a job error.

## Reviewing conflicting labels

The recommendation report measures the combined device using occupied-time recall
and an empty-time penalty. **Periods driving this result** highlights the recorded
chunks contributing most error time, with their share and a Review on graph button.
For an empty period it also shows the score without that period **at the same
thresholds**; this is not a refit or proof that its label is wrong. No suggested
period is automatically removed. Expand
**Review periods that disagree with their labels** to inspect precise error ranges
and the triggering gates. The list reserves space for quiet-presence failures as
well as empty-room bursts; it is bounded to 24 ranges and 24 session summaries.
Existing saved results gain these diagnostics after a new Learn run.

**Review on graph** opens the original labelled status and exact time range in
**Past presence labels**. The graph and draggable label bar share both their time
window and horizontal plotting edges, including after selecting a saved period,
zooming or resizing the panel. Correct the status if you know the label was wrong,
or choose **Unknown / exclude** when occupancy is uncertain, then save and learn
again. Reviewing a suggestion never changes labels or removes recordings.

A brief empty-room burst is not automatically an invalid reading. A person, pet or
other real activity may produce it. Keep known dog-only periods labelled empty
when training for human presence. If legitimate empty-room signals overlap quiet
human presence, static gate thresholds may be unable to detect people without some false activity. The old sample-count feasibility proof does not establish feasibility for this
elapsed-time objective, so new results do not use it to claim impossibility. Deleting valid difficult examples would hide
false positives. The history graph's mean line is aggregated in wider
windows; zoom into a review period to inspect its short-lived readings.

## Bugs addressed

- The missing chart threshold renderer caused populated charts to throw and could
  interrupt card rebuilding, removing subsequent cards. Chart errors are now local
  and the grid is replaced only after cards are constructed.
- Chart fetches have a timeout, ignore obsolete responses, retain a fixed window,
  and expose failures with a Refresh retry. Current buffered history is visible.
- History/timeout drafts, collapse state and chart selections survive polling.
  Pointer cancellation, component disconnect and reconnect clean up correctly.
- Uniform time sampling includes stable empty-room and quiet-presence readings.
  Automatic predictions have explicit confidence and bounded training weight.
- Retrospective corrections are idempotent, include buffered/live intervals, and
  use half-open ranges. Empty legacy snapshots no longer double-count early data.
- Earlier recommendations must be learned again with the temporal model and
  human-priority fitting model (version 1.9.2). Restart Home Assistant and reload the panel after updating. Clear stops training and invalidates learned values. Expired sessions end at the
  actual deadline; cancelling an old timeout cannot remove a replacement timer.
- Unload flushes partial history and cancels the pending save. Long gaps start a
  new history block instead of overflowing timestamp offsets. An abrupt process
  failure can still lose the current in-memory history block (up to 60 samples).
- Chart aggregation uses bounded buckets. Chart/history reads and calibration run
  on detached snapshots in HA's executor; snapshot polling no longer schedules
  unnecessary storage writes. Label lookup is indexed, decoded blocks and chart
  responses have bounded caches, and duplicate history/learning jobs are shared.
  Retrospective histogram rebuilding remains on the event loop and may be noticeable
  with a large 30-day history.
- Apply requires a complete current-model recommendation and available threshold
  configuration matching the reviewed panel state for saved results. Quality failures do not block a user-requested Apply.
  Writes are serialized per device, raise thresholds
  before lowering others, and stop/report partial failure. Concurrent Apply calls
  are rejected. Service completion is not independent hardware readback; after a
  partial failure, reread configuration and learn again before retrying.
- Websocket handlers use the current runtime after reload; snapshot access now
  matches the panel's administrator-only access.

## Verification

Install the development tools in a virtual environment, then run:

```sh
python3 -m pip install -r requirements-dev.txt
python3 -m ruff check custom_components tools tests
python3 -m ruff format --check custom_components tools tests
python3 tools/check_complexity.py
python3 -m pytest --cov --cov-report=xml --cov-report=term-missing
npm ci
npx playwright install chromium
npm run format:check
npm test
```

Python checks use synthetic data and stub Home Assistant boundaries. Coverage must
be at least 85% across the entire integration and Python tools. Every production
Python function, including nested functions, must have cognitive complexity at
most 10. No private recordings are needed by CI. On Linux, Playwright may require
`npx playwright install --with-deps chromium` to install system dependencies.

The browser harness supplies simulated HA websocket responses and checks populated
charts, three-card collapse/expand and polling, draft retention, out-of-order
requests, errors/retry, drag/cancel, confidence displays, automatic-only recommendations, focused range
changes, fixed ending timestamps, cached windows, stable refresh geometry, mobile
overflow, and reconnect. It writes
screenshots as `desktop.png` and `mobile.png` inside a private, uniquely named
`ld2410-panel-*` temporary directory. Set
`PANEL_SOURCE` to another panel file to run the same regression against it.
These checks do not exercise a deployed Home Assistant instance or physical radar.

GitHub Actions checks the local integration package, runs Hassfest, and runs the
Python, release, coverage, style, complexity, and Chromium regressions on pushes and pull requests. A successful push to
`main` publishes a GitHub release only when `manifest.json` has an increased
`major.minor.patch` version compared with the previous pushed commit. No workflow
submits this repository to the HACS catalogue. Run the local package check with:

```sh
python3 tools/validate_package.py
```

The package check verifies the directory layout, required runtime assets, JSON
files, domain and basic HACS settings. Hassfest provides the broader Home Assistant
integration validation. The remote `hacs/action` is not used: its catalogue metadata
checks are outside this project's scope, and its public manifest downloads fail
for private repositories. No license, repository description or topics are required
by the local package check; the license remains undecided.

Hosted validation runs after these files are pushed. Local checks do not establish
live Home Assistant compatibility.

## Automatic history maintenance

At startup and hourly, the integration normalizes history in Home Assistant's
executor. Valid version-1/2 blocks become version 3; version-1 observations retain unknown automatic labels
and confidence; confidence is never invented for older observations. Recorded
energies, timestamps, missing values, and human-label precedence are preserved.

Version 3 uses lossless byte-column delta encoding before zlib compression. It keeps
every recorded timestamp, energy value, missing-value marker, automatic label and
confidence value. Existing history migrates during startup/hourly cleanup; newly
recorded blocks use the same format. It does not downsample older history. Earlier
integration versions cannot read version-3 blocks; retain a pre-upgrade storage
backup if you need to downgrade.

Recording is controlled per device with **Record data**, including on collapsed
cards. Newly discovered devices start disabled. Existing devices keep recording
unless you pause them. Pausing stops history, histogram updates and automatic
training evidence, closes the current live label, and skips that device in the
overnight learning pass. Existing recordings remain available; Clear data can
remove recordings from an old placement. Re-enabling starts with an Unknown label.

**Recording storage** contains global settings shared by every device:

- Start thinning after **7 days**, initially removing confidence **below 50%**.
- Raise the minimum confidence linearly with age: **75% at 18.5 days** by default.
- At **30 days**, remove all automatic/unlabelled readings, including 100% guesses.
- Remove human-labelled readings older than **30 days** by default. This expiry
  can be later than automatic expiry, but never earlier.
- A **100 MiB total file ceiling**, configurable independently of retention ages.

Human labels override stored automatic guesses when deciding which policy applies.
The age policy deletes individual readings without changing those retained. Under
size pressure, the tuner removes compressed automatic/unlabelled chunks across all
devices, lower mean confidence first and oldest first within a confidence level.
Mixed chunks are split so their human-labelled readings remain protected. Only
after automatic chunks are exhausted does size trimming remove human chunks,
oldest first. Size pressure may remove data before its normal age expiry.

**Save settings and clean now** applies the global policy immediately. **Trim now**
uses the saved policy and can target a smaller file size without changing the
ongoing ceiling. Both actions ask for confirmation and report removed automatic
and human reading counts. Removed history cannot be restored without an export or
backup. Saved recommendations and device settings are preserved; their earlier
accuracy reports may become stale when evidence is removed.

Every integration save uses the same budget check, including recovery, overnight
jobs, shutdown and storage migration. The size includes the JSON storage envelope,
labels, summaries, settings and saved results, not just compressed history. The
panel reports the last saved file size; unfinished sample buffers are not yet part
of that file. Exports, Home Assistant backups and temporary filesystem copies are
outside this ceiling. If protected metadata alone exceeds a requested limit, the
change is rejected. If an existing limit becomes impossible, recording pauses and
the panel explains how to raise the limit or clear unused device data; no oversized
replacement is written.

Startup and hourly cleanup also remove malformed blocks and wholly unusable rows;
invalid individual gate readings become missing. Duplicate timestamps retain the
later record. Histograms are rebuilt from retained evidence. Untimed legacy human
summaries have no trustworthy original age: their retention clock starts on first
processing by this version, and they expire after one configured human-retention
period. That timestamp is not treated as an original observation time. Concurrent
sampling or label edits defer or retry cleanup instead of overwriting new evidence.

## Automatic labels from cameras, boolean entities and Bermuda

In each device's **Current presence label** section, expand **Automatic labels
from other entities**. Add one or more independent sources:

- **Boolean entity**: select a camera's human-detection binary sensor, an
  `input_boolean`, a template binary sensor, or another entity reporting `on/off`
  or `true/false`. Camera video is not processed by the tuner. Build more elaborate
  logic in Home Assistant and expose its result as a boolean entity.
- **Bermuda area**: select a person's current **Area** sensor and enter the target
  Home Assistant area ID or name. Use current Area, not Area Last Seen, Distance,
  or a home/away tracker. Area IDs keep working when the room name changes.
  Add each person's tracked device separately; choose human-carried devices, not
  pets or permanently placed beacons.

Search the Entity field by friendly name or entity ID, then tap a matching result.
Results are rendered inside the panel so selection does not depend on the browser's
native suggestion popup. Desktop keyboard users can use arrow keys and Enter;
Escape closes the results. Direct entity ID entry remains available. The list shows
up to 30 matches at once; keep typing to narrow it. Source type limits the suggestions
to boolean entities or Bermuda Area sensors, and unsaved choices survive polling.

Any positive source starts or continues a Present period. **Mark “Not Present” defaults to
OFF**: an off boolean or a person leaving the Bermuda area supplies no negative
label. If you enable it, every selected source must explicitly report absence.
Missing, unavailable, unknown or non-boolean states do not count as absence.
Only enable negative labelling when the selected sources cover everyone in the
room. Bermuda tracks a device, which may be left behind or not carried; cameras
may have blind spots. See [Bermuda's Area sensor documentation](https://github.com/agittins/bermuda/wiki).

**Trim start of presence** and **Trim end of presence** each default to **10
seconds**, configurable per device from 0 to 3600 seconds. For an observed
presence period from 12:00:00 to 12:01:00, the defaults label only readings from
12:00:10 up to 12:00:50. Periods lasting 20 seconds or less contribute no automatic
presence labels. During a longer period, labels are confirmed progressively;
the latest 10 seconds remain unlabelled until more time has passed. This also
keeps provisional boundary readings out of a Learn started during the period.

Raw energy readings remain available for graphs and manual labels. These buffers
only affect automatic Present labels from external sources; they do not delay
radar detection or change opt-in Not Present labels. Radar guessing does not
fill in the excluded positive boundaries. Set both trims to 0 to disable them.
Changes affect future periods, not historical labels. Source changes, recording
pauses, missing energy readings and sampling gaps restart the buffer. After an
integration restart, the unconfirmed tail remains unlabelled and a new observed
period starts; it is not assumed to have been continuously present while offline.

Source confidence defaults to **90%** and is configurable. This is a chosen
training confidence, not a measured accuracy claim. Below 55%, readings are
recorded but excluded from learning, like other low-confidence automatic evidence.
Sources take precedence over
radar guesses when they supply a label or are buffering presence; otherwise the
normal estimator continues.
Labels use the existing automatic evidence/retention path, including confidence
weighting and human priority. They never become human-labelled validation data.
Live manual labels and retrospective corrections still override them; mark an
incorrect recorded period Unknown to exclude it, or assign the correct status.

The panel shows the current external label and each source's availability. The
latest automatic reading identifies external sources; its radar-calibration
feedback buttons are disabled because they cannot fix another integration.
Recorded samples retain the automatic label and confidence, while the bounded
recent segment log also records external source entity IDs. There is no camera
video access, historical import, new presence entity, or automatic Apply. The
existing two-second sampling/six-second recording cadence applies; very short
source pulses between samples may not be captured. Paused recording also pauses
source-derived training. Remove all source rows and save to return to radar-only
labelling.

## Versioned releases and HACS updates

Change only `custom_components/ld2410_tuner/manifest.json` to increase the version;
the panel URL automatically uses that version to invalidate the browser cache.
After the version-changing commit is pushed to `main` and validation succeeds,
the release job creates `v<version>` at that exact commit and attaches
`ld2410_tuner.zip`. Commits with an unchanged version do not create a release.
Release jobs queue rather than cancelling earlier pending versions.

The archive contains only the runtime files explicitly listed in
`tools/validate_package.py`, with `manifest.json` at its root. Tests, local recordings,
exports, repository metadata, and credentials are not packaged. Local builds need
no credentials and publish nothing:

```sh
python3 tools/release.py
python3 -m unittest discover -s tests -p 'test_release.py'
```

Uploads are completed while the release is a draft. Reruns verify existing tags
and asset hashes instead of replacing published content. A failed upload can be
retried by rerunning its original workflow. A version rollback or reuse for another
commit fails explicitly. Use another version for changed release contents.

HACS detects published release tags and downloads the configured ZIP asset; the
repository must be public and added as a custom integration repository. HACS update
availability does not itself schedule installation or restart Home Assistant.
For unattended installation, configure the integration's HACS update entity with
Home Assistant's `update.install` action and your own restart policy. See the
[HACS version rules](https://www.hacs.dev/docs/publish/start/#versions) and
[update entity documentation](https://www.hacs.dev/docs/use/entities/update/).
Publishing a GitHub release does not add this integration to the HACS catalogue.
The license remains undecided.

### Comparing saved thresholds

Each device header shows **Current** (the actual live gate settings) and **Best** (the best compatible saved pattern). Open **Recommendations → Compare saved patterns** to compare User learnt, Previous, Current and Autolearnt. The saved Current slot is the last applied result; it can differ from live settings after another tool changes the radar. Review selects a saved pattern; Apply remains a separate manual action here; enabled overnight learning can independently apply a freshly assessed improvement.

The 0–100 comparison score is `max(0, 100 − 5 × missed occupied-time percentage − false-active empty-time percentage)`. It uses the existing learner’s time penalty, with the conservative occupied-time estimate where onset is uncertain. 100 requires no measured errors; zero includes all costs at or above 100. This is a comparison score, **not measured deployment accuracy or a confidence percentage**. The 99.9% presence target is shown separately. Sample outcomes, durations, timing assumptions and exclusions remain available in each row.

Scores and their scorer version are persisted with the device. An existing score is reused after page visits and integration restarts; only a scorer-version change automatically recalculates that configuration. A newly encountered threshold configuration gets a score once. Identical thresholds and active entities share one record across live settings and saved slots. At most five distinct configurations are retained per device, with no extra recording copies. Clear data removes these scores too.

New recordings, corrected labels, retention changes and timing-policy changes do not silently alter a stored score. Its date and original timing describe its assessment. Scores calculated together use the same evidence; scores from different dates may use different evidence or timing and are **not a controlled head-to-head comparison**. Best ranks the stored assessments, including unrounded costs and duration tie-breaks. Incompatible results are excluded from Best.

Calculations use up to the latest 5,000 observations per state and source, complete gate vectors and the existing outlier filter. Human evidence takes priority; confidence-weighted automatic evidence fills unsupported states and breaks ties. Automatic evidence is identified as estimated. Insufficient evidence is **unscored**, never a fake zero. **Check stored scores** reuses valid scores and retries unscored or failed assessments; it does not refresh already-scored configurations against newer recordings. Checking stored comparison scores never applies thresholds or initiates radar recovery.

For maintainers: increment `SCORER_VERSION` in `calibration/comparison.py` whenever score calculation, evidence selection, filtering or timing-replay semantics change. A frontend-only release does not invalidate saved scores.


## Continuing room adaptation

Version 1.16.0 adds a separate, persisted room-reference profile for automatic
labelling. Human labels and configured entity statuses keep teaching it after
initial setup. The hardware still detects presence from its gate thresholds;
Learn and Apply work as before and no thresholds are applied automatically.

- Human labels take precedence over entity or inferred labels on the same
  recording. Human, entity and radar-derived references remain separate.
- Confirmed entity labels teach future radar guesses at their configured
  confidence. The start/end buffers apply before a positive label enters the
  profile; short excluded periods cannot teach it. Unavailable sources do not
  become empty-room examples, and negative entity labels remain opt-in.
- Recent reference periods have more influence. The age weight blends a
  seven-day half-life (80%) with a sixty-day half-life (20%). Source weights are
  human 1, entity 0.5 and inferred 0.1. Each period's influence is capped, so one
  long recording cannot supply unlimited independent evidence. Recent entity
  evidence can outweigh an old human distribution.
- Radar-derived references require a confirmed estimate of at least 80%
  confidence and existing independent reference evidence. They cannot create
  independent support or raise its confidence cap. A continuously occupied room
  is not deliberately reclassified as empty simply because readings are steady.
- Unfamiliar channel energies reduce estimated confidence and show a review
  message in **Automatic analysis & feedback**. A new fan and quiet human can
  produce ambiguous radar evidence; a known human/entity status can resolve it.
  High-confidence inference remains heuristic, not a measured accuracy claim.

The profile keeps compressed gate distributions, not another copy of the raw
recordings. It retains at most 24 periods per source and state (144 total),
splitting continuous periods at 30 minutes. It survives recording expiry and
integration restarts, and its bytes count towards the global storage ceiling.
Clear data also clears the profile. Existing retained human recordings are
imported once at startup, using their recording timestamps. Histogram-only legacy
training is retained at reduced confidence, explicitly marked as having an unknown
observation age; its timestamp is the import age, not an invented recording date.
Historical automatic
labels have no reliable source provenance and are not imported as entity truth.

Editing a past human period rebuilds the retained human references. Any entity
or inferred summary overlapping the edit is removed in full, since its aggregate
cannot be precisely unlearned one sample at a time. Older, expired human summaries
remain age-weighted references. Changing the entity source configuration resets
its entity and inferred reference contributions while preserving human references.
Pausing recording pauses reference training too. Profile summaries and unfamiliar
readings appear inside the existing automatic-analysis card.

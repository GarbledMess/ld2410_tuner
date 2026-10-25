# LD2410 Tuner

Home Assistant custom integration for labelled radar calibration and gate history.

[Release notes](CHANGELOG.md) - 1.30.1 stops labelling history from freezing Home Assistant and applies the first label after a restart at once.

All collapsible sections start closed. Each heading describes what is inside;
sections you open stay open during panel refreshes. History and Recommendations
shortcuts open the relevant device section directly.

## Installation

### Manual installation (private repository)

Copy only the repository's `custom_components/ld2410_tuner` directory into
`<Home Assistant config>/custom_components/ld2410_tuner`, then restart Home Assistant.
Add **LD2410 Tuner** under **Settings → Devices & services → Add integration** and
refresh the browser fully to load the panel. Do not copy the repository root into
that folder. Existing stored training/history are preserved; earlier recommendations
must be learned again before Apply. Local brand icons are supported by Home Assistant
2026.3+.

### Back up before upgrading to 1.24 (history moves to day files)

**Upgrading to 1.24 moves the recorded history out of Home Assistant's JSON store, and
the move cannot be undone by installing an older version.** The first start after the
upgrade converts every stored history block and automatic guess run into day files in
`<config>/.storage/ld2410_tuner/history/` (see [Recorded history storage](#recorded-history-storage)),
then saves `.storage/ld2410_tuner.data` without them. Labels, room labels, settings,
references and saved results stay in the JSON store unchanged.

- **Downgrading afterwards loses the recordings.** An older version reads only the JSON
  store, finds no history there and does not read the day files. Anything it records is
  kept in the JSON store again, and upgrading once more converts that and *replaces* the
  day files of each radar it converts, so recordings made by 1.24 before the downgrade are
  lost for those radars.
- **Back up first.** Before upgrading, take a Home Assistant backup (or copy
  `.storage/ld2410_tuner.data`) and keep it for as long as you may want to go back. To
  downgrade, stop Home Assistant, restore that copy, delete the
  `.storage/ld2410_tuner/` folder, then install the older version.
- The conversion runs in a resource-limited worker at startup and keeps each stored
  reading at its own time (old recordings keep their six-second resolution; nothing is
  invented between samples). If it is interrupted, the next start converts again from the
  untouched JSON store. Old summaries without timestamps cannot be placed on a day and are
  dropped; earlier learned results are then marked for relearning.

This applies when upgrading from any earlier version, including 1.22.

History preparation runs at low priority after integration setup returns, so a large
conversion does not hold up Home Assistant's startup. The panel appears and recording
starts when preparation finishes; no manual migration step is needed. Expensive numerical
and storage work runs in bounded, low-priority Linux subprocesses with mandatory memory,
CPU-time and wall-time limits. Periodic polling yields between radars. Unsupported
resource controls or oversized jobs fail explicitly; no unprotected fallback is used.

### HACS custom repository (requires a public GitHub repository)

This project uses the HACS-compatible folder layout but is not being submitted to
the HACS default catalogue. [HACS cannot install private repositories](https://www.hacs.dev/docs/faq/private_repositories/),
so use manual installation while this repository remains private.

If you later choose to make the GitHub repository public, it can be added manually
in HACS under **Custom repositories** using
`https://github.com/GarbledMess/ld2410_tuner` with type **Integration**. Download
**LD2410 Tuner**, restart Home Assistant, and add the integration as above.
Adding a custom repository does not require submitting it to the default catalogue.

## Rooms, zones and shared radars

The **Rooms and zones** card is organized as a layout tree: each Home Assistant
area, the zones you define inside it, **Elsewhere in <area>** for the area's radars
that no zone claims, area-less groups, and finally **Unassigned** radars. Areas
supply default groups containing the area's recording-enabled radars. **Edit
members** overrides that selection; **Restore area default** restores it. **Add
zone / manual group** creates an independently assessed selection, optionally
organized under an area. These settings do not change Home Assistant's registry or
require the optional ESPHome package.

For example, **Office** can contain **Desk** and **Sofa** zones, each with its own
radar selection; radars in Office that neither zone uses appear under **Elsewhere
in Office**. The Office assessment can cover both zones, while the zone
assessments stay separate: a person at the desk does not count as sofa
occupancy, and a desk detection cannot hide a sofa miss. Zones that share a radar
still keep their own room truth and cost. A radar that covers both the desk and the sofa can be
selected in both zones and then appears under each; it is learned once, together
with the linked zones (see Learning sets below). Grouping cannot locate a person
within a radar's coverage. You can label a room or zone directly, and each radar is
credited with what it saw, as described next. Area membership for manual zones is
organizational: the whole-area default still uses the actual device areas, unless
you explicitly edit its members.

Review old labels that described a wider area before using them for a narrower
zone. No labels are propagated from an area into its zones except by the
implications below.

### Room labels and how a room's truth is decided

Each group header in the tree holds that group's controls and a **How rooms and
zones work** help.

You can label a room or zone Present, Empty or Unknown directly on its timeline.
These manual room labels are a separate record from each radar's own labels.
A room or zone with exactly one radar is that radar: its manual labels train the
radar as the radar's own labels would, and the radar's own labels win where both
exist. **Seen by** still shows what the radar's own energy and labels say.
Sensor-derived room labels come from optional room presence sources (camera,
boolean entity or Bermuda) and are stored separately; a sensor label change only
marks the members' references out of date (see below). Editing a manual label
detects stale edits through its own revision, independent of the sensor revision.

For any group, truth is decided in this order, first match wins:

1. A human label on that room or zone itself. An explicit Unknown suppresses
   automatic fallback.
2. Implied truth: a Present zone makes its area Present; an Empty area makes its
   zones Empty.
3. Truth derived from member groups.
4. Automatic evidence: sensor labels, then the room estimate.

The room estimate combines members with a noisy-OR of their calibrated guesses
(see **Which automatic guesses count**). A present estimate needs two members to say
present (every member of a group of one); a lone radar's guess is not a room's. Its
**Was this right?**
feedback is handled on the server and affects at most one hour per answer.

### Which radar saw it

A radar's own label means "should this radar detect here". Tap a **Seen by** chip on
an occupied period to correct it; that saves a label on that radar.

Each radar is credited with a room's occupied time by one function, shared by the
learner and the **Seen by** chips:

- If the radar has its own human label, that label decides.
- Otherwise the radar is credited when its energy stays above its own empty
  baseline (99th percentile of its empty-labelled energy plus a margin of 3) for
  at least 30 seconds. This is an automatic guess, ignored below a confidence of
  0.55, that reaches full strength with 100 baseline samples. The 30 second
  sustain was chosen over 18 seconds on recorded data to reduce false attribution
  on empty periods.

All automatic evidence, including a radar's own guesses, the room estimate, sensor
room labels and these attributions, trains at its confidence times the **Automatic
evidence weight** setting (default 0, up to a quarter of the human evidence at 1).
Human labels have weight 1. Automatic evidence is therefore used but stays clearly
down-weighted, and it is not independent validation of the detector.

### Which automatic guesses count

A radar's recorded confidence is the age of its newest reference, not a probability.
On the supplied recordings every `not_present` guess was right (45,000+ labelled
samples on four radars) while `present` guesses were right 6-88% of the time, almost
only wrong in long stretches at the energy of an empty room. Every consumer of
guesses (the learner, the room estimate, Seen by, the assessment and the radar's own
reference training) therefore reads them through one function, `guesses.evidence`:

- A `not_present` guess counts at a fixed calibrated confidence of 0.95.
- A `present` guess counts only when its recorded confidence says the reference was
  fresh (under about six hours old) or absent; between 0.70 and 0.975 it counts for
  nothing. Its calibrated confidence is 0.95.
- A `present` guess does not count where every gate reads within the ceiling of the
  radar's own Empty-labelled readings (their 99.9th percentile plus 3). A radar with no
  Empty labels has no ceiling and nothing to veto with.
- A room is present on automatic evidence only when two members say so.
- A radar's own guesses still count: they train its references and its learner
  evidence under the same rules. Sources outside the radar (entities, room sensors)
  keep the confidence they were given.

The role report shows per radar: **Covers alone**, **False-active** and **Missed
what it saw** (occupied time attributed to the radar but not detected, split into
inferred and automatic parts). **Radar on its own** keeps the individual learner
score, a different measure that is not comparable with the room figures.

**Assess current settings** replays 24 hours, 3 days or 7 days of recordings.
Each radar's own timeout and configured delay policy are replayed first; detections
are then combined. Any member may detect presence, and any member may cause a
false positive. Overlapping detections count once. Only shared recording coverage
is assessed; missing readings, gaps and timing warm-up are excluded and reported.
A history window limits the assessed dates, without deleting recordings.

The result includes the combined score, occupied/empty time, hits, misses, false
triggers, correct empties, and each radar's exclusive occupied-time contribution.
All valid readings are included.
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

### Learning sets

**Learn together** fits all radars of a learning set against the last seven days of
shared recordings. A set is a group of linked zones (its `set_id` is the sorted
group ids joined with `+`; a single-group set is just that group). Radars shared
between zones get one threshold value. Zones that cannot be scored are skipped and
reported. Device Learn, set-level jobs, Apply and overnight learning operate on
whole sets; overnight learning visits each set once and shares its outcome with
all members.

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
penalty on the same fresh evidence, and is refused when any zone was skipped or
would get worse. A disabled member blocks automatic application for the whole set.

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
An overnight pass may start during the 15 minutes following the configured time.
If Home Assistant restarts or the schedule is enabled outside that window, it waits
for the next scheduled day instead of catching up during the day. The window limits
when a pass starts; a started pass may finish after it closes. A persisted daily
marker avoids duplicate runs after restarts or during the repeated autumn clock-change
hour. A skipped springtime clock hour uses the first valid minute after the jump,
with the same 15-minute dispatch window. Disabling the schedule lets an
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

The learner needs at least five minutes of observed time in each state (shown as 50
six-second units), combining human labels and confident automatic estimates. There is no
minimum human percentage and no human-only quota for Apply. Missing gate readings remain
missing; readings from other gates still contribute. Gates with no observations keep
their current threshold.

**Learning evidence budget.** A radar whose retained history fits 50,000 recorded changes
is learned from all of it. A denser history (a busy radar can record hundreds of thousands
of changes a day) is learned from representative recorded periods instead of a random
sample: each class's newest time, its quietest presence and loudest empty-room noise, then
the minutes either side of label edges (newest first, while they fit) are kept whole; the
rest of each class is sampled evenly across the whole history, and each sampled period
stands for the time it represents, so totals and rates stay unbiased. Classes are kept apart by state and source
(human or automatic). Presence episodes and automatic-label quality are still counted from
the whole history. The result says when the budget was used; its time totals are then
weighted estimates. If the configured device timing needs more context than the budget
allows, the result is insufficient rather than silently partial.

History records every gate value change at the time Home Assistant delivers it, and
nothing between changes: a reading holds until the radar reports a different one (see
[Recorded history storage](#recorded-history-storage)). Learning and histograms weigh
readings by how long they held, not by how many were recorded. The learning result reports
the human and automatic recorded time actually used, separately from gate histogram counts.

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
**Export JSON** downloads a file holding the radar's settings, statistics, learning results
and every recorded day (lossless day files: weeks of history can be several megabytes).
Home Assistant writes it and keeps it for 10 minutes, the life of its download link.
**Export CSV** holds one row of statistics and recommendations per gate.
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

The **Room presence** graph uses the same view for every radar in the room on one
chart: all nine gates of the selected kind (Movement or Still), each in its gate
colour, with line style identifying the radar. Select a G0–G8 button to highlight
that gate's average line and recorded range for each radar, together with each
radar's current threshold for it. A radar with no threshold for that gate shows none.
Graphs and recorded-data reads use their own low-priority worker, separate from
learning, so they never wait behind a running Learn. Each worker runs one job at a
time under the same CPU and memory limits.

## Recorded data

Each radar card has a **Recorded data** section, closed by default, showing exactly
what is stored for that radar. Its open state survives polling, and each radar keeps
its own day, page and export progress. It lists the
recorded days with their actual disk sizes, plus any recovery files retained from
an interrupted storage transaction, which are counted separately. Opening a day
shows its stored rows: start and end times to the millisecond (UTC), the duration,
and the nine gate values of the selected kind exactly as stored, without averaging
or rounding. A gate with no recorded value shows **Missing**; gaps between rows are
not filled. Rows are shown 100 at a time.

**Download day CSV** writes every row of the day, with movement and still values
for all nine gates; missing values are empty cells. The download reads the day in
pages pinned to one snapshot, so it never mixes two versions of a day. If the
recording is rewritten meanwhile, the export stops and asks you to reload. Progress
is shown and **Cancel export** (or leaving the panel) stops it without saving a
partial file. A dense day takes roughly a minute or two on a Home Assistant Green.

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
98%. These scores are **heuristic**, not empirically calibrated probabilities; evidence
uses the calibrated confidence described under **Which automatic guesses count**.
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

Automatic guesses are stored as runs with their label and confidence beside the gate
energies. Confidence weights the observed time each guess covers; extra readings within a
brief fluctuation do not multiply its influence. Automatic scores refine candidates after
human-labelled performance and supported separation. Human priority is enforced by
ranking, not by the relative volume of estimates. Sample counts shown in calculation
details are observed seconds divided by six. Scores below 55% are not used. Converted
history without confidence gains no invented estimates. Human corrections and
explicit UNKNOWN ranges override stored guesses. Feedback buttons adjust estimator
bias; use chart/training labels to supply known occupancy examples.

Learning uses every recorded reading in the evidence it learns from: it does not exclude
brief dips or spikes as outliers. Empty-room spikes and quiet presence both remain evidence,
so a brief dip during labelled presence can make a result unsafe rather than being removed.
Original recordings and labels remain unchanged.

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

Only recorded coverage counts: gaps and ends of coverage do not invent minutes of
evidence, and a reading holds only until the next change. Unknown device timing is marked
unknown. Human labels are unchanged; no observations are removed to improve a score.

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

When the labelled episodes allow it, learning first checks its policy chronologically: it
cuts the history between whole episodes, near 80% of them, with both states on each side;
learns two candidates from the earlier part (one where automatic evidence only breaks ties,
one that also weighs quality-corrected automatic evidence); and keeps the automatic
evidence only if, on the later part, it improves missed or false-active time without
missing more presence, more empty time or more empty-room episodes; a tie keeps the
tie-break policy. The final recommendation is
then learned from **all retained** evidence with the chosen policy. The earlier-data
check is reported with the result. Without a valid cut, the corrected policy is used and
the result says the choice was not validated.

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
- Unload writes the unwritten history changes and cancels the pending save. An abrupt
  process failure can lose only the changes recorded since the last write; coverage
  then ends at the last stored change.
- Chart aggregation uses bounded buckets. Chart/history reads and calibration run
  in HA's executor; snapshot polling no longer schedules unnecessary storage writes.
  Label lookup is indexed, decoded days and chart responses have bounded caches, and
  duplicate history/learning jobs are shared. A label edit recounts only the time whose
  label changed.
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
python3 -m bandit -r custom_components tools
npm ci
npx playwright install chromium
npm run format:check
npm test
```

`pre-commit install` adds a git hook that runs ruff, Bandit, prettier, gitleaks and
basic file checks (including LF line endings) on each commit; `pre-commit run
--all-files` runs them on demand. Hook versions match `requirements-dev.txt` and
`package.json`. Bandit fails on any finding; an intentional exception needs a
`# nosec <test id>` comment with its reason on the line above.

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
Python, release, coverage, style, complexity, and Chromium regressions on pushes and pull requests.
It also runs Bandit, a zizmor audit of the workflows (every action is pinned to a
commit) and a redacted gitleaks scan of the full history; these gate releases too.
CodeQL (security queries), pip-audit and npm audit run on pull requests, `main` and weekly, and report
without gating releases. Dependabot proposes weekly pip, npm and action updates.
SonarQube Cloud analysis runs only once a `SONAR_TOKEN` repository secret exists.
A successful push to
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

## Recorded history storage

Recorded history lives in its own folder beside the JSON store, so Home Assistant
backups include it:

```text
<config>/.storage/ld2410_tuner/history/<radar device id>/<YYYY-MM-DD>.log   today (UTC)
<config>/.storage/ld2410_tuner/history/<radar device id>/<YYYY-MM-DD>.day   finished days
```

- **What is stored.** Every change of a gate energy, at the time Home Assistant delivered
  it (millisecond resolution), and nothing else: no snapshots, no heartbeats. A reading
  holds until the next change. Changes delivered in the same millisecond are one record.
  The radar's automatic guess is stored in the same day as **guess runs** (one record per
  stretch of time with the same state, source and confidence step), so one day file holds
  all of a radar's time evidence.
- **Coverage.** Recording starts with the radar's full current readings and lasts until
  an explicit end: recording paused, the radar removed, storage blocked, a configuration
  recovery or the integration stopping. An unavailable or non-numeric entity makes only
  that gate missing (missing is never zero). After a crash, coverage ends at the last
  stored change. The two-second poll records only values that events missed, so it never
  duplicates them.
- **Today's file** is append-only: changes are kept in memory and appended at the next
  save and when the integration stops; nothing is rewritten. A partly written last record
  is ignored.
- **Finished days** are compacted once into a zlib-compressed columnar file (one byte per
  gate per change; a gate that reported non-integer values that day uses a float column).
  They change only when thinning or **Trim now** cuts time out of them.
- **Summary files** (`.sum`) beside finished days are optional indexes that make label
  counts and reference rebuilds fast. They hold no history of their own, are limited to
  64 MiB, are rebuilt when missing or damaged, and are the first thing removed under the
  size ceiling.

Values are stored as received; rounding is the job of the ESPHome package (or whichever
integration provides the entities).

## Automatic history maintenance

Once a day (checked hourly and at startup; the time of the last cleanup is saved, so a
restart does not repeat it), the integration applies the retention policy below in the
background. Thinning removes whole **time ranges** from finished days (the cut
time becomes an explicit gap), never single changes; today's file is never thinned. Human
labels decide which policy applies to a stretch of time; otherwise its automatic guess run
and confidence do. A label edit, thinning or **Trim now** recounts only the time that
changed.

Background work is kept light so it never burdens Home Assistant: recording appends only the
changed readings to today's log every few seconds and saves the integration's own state every
five minutes; live reference evidence is folded once a minute per radar, and radars take
turns; heavy jobs (the first cleanup after an upgrade, label recounts after a restart,
reference rebuilds and learning) rest between steps so they use at most about half of one
CPU core. On a dozen radars, steady recording costs well under 1% of one core on a desktop.

The radar's guess is not recomputed on every two-second tick. A radar's readings are
classified again only when a channel has stayed 3 energy units or more from the readings
last classified for 5 seconds, its references, calibration or external source changed, the
filter has not settled, or a minute has passed; otherwise the last classification stands
(and stays current for the panel and the Correct/Incorrect feedback), and the temporal
filter and confirmation continue over the elapsed time.

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
The age policy removes time ranges without changing the time retained. Labels older than
the human retention expire with the time they cover. Under size pressure, the tuner
removes automatic/unlabelled time across all devices, lower confidence first and oldest
first within a confidence level; human-labelled time inside a day stays protected. Only
after automatic time is exhausted does size trimming remove human-labelled time, oldest
first. Size pressure may remove data before its normal age expiry.

**Save settings and clean now** applies the global policy immediately. **Trim now**
uses the saved policy and can target a smaller file size without changing the
ongoing ceiling. Both actions ask for confirmation and report removed automatic
and human-labelled recorded time. Removed history cannot be restored without an export or
backup. Saved recommendations and device settings are preserved; their earlier
accuracy reports may become stale when evidence is removed.

Every integration save uses the same budget check, including recovery, overnight
jobs, shutdown and the conversion. The size is the history folder (day files, today's
log and summary files) plus the JSON store with its labels, settings and saved results.
The panel reports the size at the last save; changes not yet appended are not counted. Exports, Home Assistant backups and temporary filesystem copies are
outside this ceiling. If protected metadata alone exceeds a requested limit, the
change is rejected. If an existing limit becomes impossible, recording pauses and
the panel explains how to raise the limit or clear unused device data; no oversized
replacement is written.

At startup, leftovers of an interrupted day finish are removed and earlier days' logs are
finished. Histograms are rebuilt from retained recorded time. Concurrent recording or
label edits defer or retry cleanup instead of overwriting new evidence.

## Automatic labels from cameras, boolean entities and Bermuda

Each radar's **Current presence label** section contains its presence-source settings.
For cameras and other boolean entities, expand **Automatic labels from other entities**
and add independent sources: a camera's human-detection binary sensor, an
`input_boolean`, a template binary sensor, or any entity reporting `on/off` or
`true/false`. Camera video is not processed by the tuner. Build more elaborate
logic in Home Assistant and expose its result as a boolean entity.

### Bermuda

The top-level **Bermuda** section, after the radar cards, starts closed and remembers
its open state during polling. It lists tracked phones and watches by friendly name
and current area. Discovery uses Bermuda Area sensors with a matching range sensor
in Home Assistant's entity registry, so renamed entity IDs work too.

For each tracked device, turn on the radars it should label. Each selected radar uses
its own device-registry **Home Assistant area**, rather than a manually chosen area.
A radar without an area has disabled controls explaining how to enable them.
Optionally enter **Range (m)** per radar (greater than zero, up to 100 m); blank
accepts any distance. Click **Save Bermuda settings** to apply the assignments.
Unsaved choices survive polling. Use human-carried devices, not pets or permanently
placed beacons.

The phone or watch must be in the radar's area. Within that area, the optional range
limit uses the matching Bermuda range sensor. Missing or unusable range gives no
label; exceeding the limit counts as outside the area for negative labelling.
The enabled devices combine with the radar's other sources using its source
confidence, **Mark “Not Present”**, and trim settings described below. These radar
settings remain available for assigned radars even when they belong to a room.
Room-level source configuration is separate and unchanged.

On the first startup after upgrading, existing per-radar Bermuda sources targeting
the radar's own area (by ID or case-insensitive name) move to this section with their
range limits. Sources intentionally targeting other areas stay in the radar's editor
and continue working; they can still be edited or removed. New per-radar sources use
the Boolean type; configure new tracked devices in **Bermuda**. Room-level editors
still allow Bermuda sources with their own explicit target area.

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
Recorded guess runs retain the automatic label and confidence, while the bounded
recent segment log also records external source entity IDs. There is no camera
video access, historical import, new presence entity, or automatic Apply. Sources are
read on the two-second poll, and the guess is recorded every five seconds; very short
source pulses between polls may not be captured. Paused recording also pauses
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

Calculations use the same recorded evidence as Learn, including its evidence budget on dense histories. Human evidence takes priority; confidence-weighted automatic evidence fills unsupported states and breaks ties. Automatic evidence is identified as estimated. Insufficient evidence is **unscored**, never a fake zero. **Check stored scores** reuses valid scores and retries unscored or failed assessments; it does not refresh already-scored configurations against newer recordings. Checking stored comparison scores never applies thresholds or initiates radar recovery.

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
- Radar-derived references require a guess that counts as evidence (calibrated
  confidence of at least 55%, a present one not vetoed as quiet) and existing
  independent reference evidence. They cannot create
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
Reference timestamps remain numeric seconds. Batched rebuilding can serialize an
integral timestamp as a JSON floating-point number (for example, `120.0` instead
of `120`); its value and the history schema are unchanged.
Clear data also clears the profile. Existing retained human recordings are
imported once at startup, using their recording timestamps. Histogram-only legacy
training is retained at reduced confidence, explicitly marked as having an unknown
observation age; its timestamp is the import age, not an invented recording date.
Historical automatic
labels have no reliable source provenance and are not imported as entity truth.

Editing a past human period removes any entity or inferred summary overlapping
the edit in full, since its aggregate cannot be precisely unlearned one sample at
a time. Rebuilding the retained human and room references reads the radar's whole
history, which takes seconds on a small host, so edits never do it: device and
room label edits, Seen by taps, membership, recording and registry changes and
room sensor labels only mark the radar's references out of date. The automatic
card then shows "References out of date since …". Out-of-date references rebuild
in the background once a day at the learning schedule time: at the start of the
overnight learning pass, or on their own when overnight learning is off. Use
**Refresh references** on that card to rebuild at once; the card shows "Refreshing
references…" until it finishes. Learn does not read references, so it does not
wait for them. Older, expired human summaries remain age-weighted
references. Changing the entity source configuration resets
its entity and inferred reference contributions while preserving human references.
Pausing recording pauses reference training too. Profile summaries and unfamiliar
readings appear inside the existing automatic-analysis card.

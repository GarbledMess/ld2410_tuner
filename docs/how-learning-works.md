# How learning works

This is the detailed companion to the [README](../README.md). It describes what the
tuner counts as evidence, how a room's truth is decided, how candidates are scored and
searched, and what the results do and do not prove.

## Evidence

### Human labels

Human labels have weight 1 and always take priority. They are never changed or removed
to improve a score. An explicit **Unknown** excludes its period from every training
source, including automatic guesses.

A radar's own label means "this radar should (or should not) detect here". A room or
zone label describes the room; how it reaches each radar is described under
[Room truth and attribution](#room-truth-and-attribution).

### Automatic evidence

All automatic evidence – the radar's own guesses, the room estimate, entity and Bermuda
labels, and Seen by attributions – trains at its confidence times the **Automatic
evidence weight** setting (default 0; at 1 it carries up to a quarter of the weight of
human evidence). Evidence below 55% confidence is recorded but not used. Automatic
evidence is used, but stays clearly down-weighted; it is not independent validation.

### The radar's own guesses

The automatic estimator compares each gate against the human-labelled occupied and
empty distributions using smoothed likelihoods, adds nearby-gate support, and runs a
two-state temporal filter: presence needs about four seconds of continuing support to
confirm, absence about eight, and gaps over ten seconds reset it. Move and still at the
same gate are treated as correlated, not two votes. Missing channels cannot certify
absence.

Before human labels exist it warms up a background estimate, which stops adapting
during likely occupancy so a still person is less likely to become the baseline.
Confidence is capped at 65% without human references, 80% partly guided and 98% fully
guided. These are heuristics, not calibrated probabilities. A flat start-up stays
Unknown until there is variation or a human Empty label; a room that is occupied
during start-up is ambiguous until a human labels it.

A radar's guess is not recomputed on every two-second poll: it is reclassified when a
channel has stayed 3 or more energy units from the last classified readings for 5
seconds, its references or sources change, the filter has not settled, or a minute has
passed. Otherwise the last classification stands while the filter keeps running over
the elapsed time.

### Which guesses count

A guess's recorded confidence is the age of its newest reference, not a probability.
On the supplied recordings every `not_present` guess was right (45,000+ labelled samples
on four radars), while `present` guesses were right 6–88% of the time, almost only
wrong in long stretches at empty-room energy. Every consumer of guesses (the learner,
the room estimate, Seen by, assessments and reference training) therefore reads them
through one function, `guesses.evidence`:

- A `not_present` guess counts at a calibrated confidence of 0.95.
- A `present` guess counts (at 0.95) only when its reference was fresh (under about six
  hours old) or absent; a recorded confidence between 0.70 and 0.975 counts for nothing.
- A `present` guess is vetoed where every gate reads within the ceiling of the radar's
  own Empty-labelled readings (99.9th percentile plus 3). A radar with no Empty labels
  has no ceiling.
- Each guess source (state, recorded confidence and origin) is checked against human
  labels for that radar: its own, else those of a room or zone it alone records in,
  else Empty where a room or zone covering it is labelled Empty. Labelled time is cut
  into hour-long trials, and a source counts only while it is right at least 55% of
  the time at even odds. One right trial is assumed in its favour, so it takes about
  five labelled hours of being wrong to discard a source. A discarded guess is no
  evidence anywhere, and in a room it neither votes nor vetoes.

Sources outside the radar (entities, room sensors) keep the confidence they were given.

### Room references

Each radar keeps a persisted reference profile that the estimator uses. Human labels,
entity labels and the radar's own counted guesses feed it as separate sources (weights
1, 0.5 and 0.1). Recent periods count more: the age weight blends a seven-day half-life
(80%) with a sixty-day one (20%), and each period's influence is capped, so one long
recording cannot dominate. Radar-derived references need existing independent evidence
and cannot raise the confidence cap.

The profile holds compressed gate distributions, at most 24 periods per source and
state (144 total), split at 30 minutes. It survives history expiry and restarts and
counts towards the storage ceiling. Unfamiliar channel energies lower confidence and
show a review message in **Automatic analysis & feedback**.

Rebuilding references reads the radar's whole history, so edits never do it directly:
label edits, Seen by taps, membership, recording and registry changes only mark the
references out of date ("References out of date since …"). They rebuild once a day at
the learning schedule time, or at once with **Refresh references**. Learn does not read
references, so it never waits for them. Editing a past human period drops any entity or
inferred summary overlapping it. Changing a radar's entity sources resets their
contributions, keeping human references.

The **Was this right?** / Correct/Incorrect feedback adjusts estimator bias and affects
at most one hour per answer. It is disabled when the latest label came from an external
source.

## Room truth and attribution

For any room or zone, the first rule that applies decides:

1. A human label on that room or zone. An explicit Unknown suppresses the fallbacks.
2. Implied truth: a Present zone makes its area Present; an Empty area makes its zones
   Empty.
3. Truth derived from member groups.
4. Automatic evidence: sensor labels, then the room estimate.

The room estimate combines members' calibrated guesses with a noisy-OR. Present needs
two members to say Present (every member of a one-radar group); a lone radar's guess is
not the room's. No labels are copied from an area into its zones except by rule 2, so
review old labels that described a wider area before relying on them for a zone.

In a room or zone with exactly one radar, manual room labels and sensor room labels
train that radar directly (manual at human weight, sensors at their confidence); the
radar's own labels win where they overlap.

### Seen by

A room's occupied time is credited to its radars by one function, shared by the learner
and the **Seen by** chips:

- A radar's own human label decides, if it has one.
- Otherwise the radar is credited when its energy stays above its own empty baseline
  (99th percentile of its Empty-labelled energy plus 3) for at least 30 seconds. This
  is an automatic guess at confidence 0.55 or more, reaching full strength with 100
  baseline samples. 30 seconds was chosen over 18 on recorded data because it
  attributes less on empty periods.

An Empty room is Empty for each of its radars. Tapping a Seen by chip saves a label on
that radar.

## Scoring

### Detection model

A candidate detects presence when any enabled gate's energy is **strictly above** its
threshold, following the
[ESPHome threshold contract](https://esphome.io/components/sensor/ld2410/#number). An
empty-room trigger from any gate is a device false positive; a per-gate miss is not a
device miss. A threshold of 100 turns a gate off.

All active threshold entities must be exposed. Exposed maximum-distance settings limit
the modelled gates; when they are absent, gates 0–8 are assumed.

### Error cost

```
error cost = M × missed occupied-time % + false-active empty-time %
```

`M` is 5 for **Balanced** and is scaled by the room's
[presence priority](../README.md#room-settings) (1 to 50). Each percentage uses its own
class's observed duration, so recording far more empty time than occupied time does
not drown out presence. Lower is better. Missed time uses the conservative end of the
timing range below. Automatic evidence uses the same cost with confidence-weighted
time, after human evidence. Episode coverage, recent-time cost and event counts break
ties.

The quality target is **99.9% of occupied time**. It is not a free allowance for misses:
a cheaper compromise that misses it is marked red, never presented as meeting it. The
displayed false-positive score is 0 at best; −1 means 1% of empty time falsely active.

A result is **red** when it misses the target, misses a whole measurable occupied
session, or stays active throughout empty time; **amber** for unknown timing, limited
human evidence or a backtest concern; otherwise **green**. All quality states are
advisory for a manual Apply.

### Presence priority

The room's priority multiplier scales every presence trade-off Learn makes for it: the
cost of a missed second, the price paid when lowering thresholds to catch quiet
presence, how much labelled recall a candidate may give up against the best one, and
the quiet-presence prior. Above ×1 a candidate may also be mostly lit in more empty
periods than the current thresholds; below ×1 it may mostly miss more presence episodes.
Zones learned together keep their own priorities. Reports show the cost each room used.

### Device timing

Learn reads the radar's **Timeout** and, if both are exposed, the ESPHome `delayed_on`
and `delayed_off` presence filters (the optional package publishes them as diagnostics;
[standalone configurations work too](esphome-recovery.md#exposing-timing-for-calibration)).
Gates are combined first, then the timeout and known delays are replayed. A gap covered
by the hold costs nothing; an on-delay only rejects an activation still too short after
the hold.

Recordings are change-based but effectively sampled. For empty time, possible activity
between neighbouring observations is included, so a lone reading is not assumed to be
a harmless spike. An on-delay transition can start anywhere between the last low and
first high reading, so missed presence is reported as a range. Replay restarts at label
changes and gaps over twelve seconds; the first observations, up to the larger of
timeout + off delay and on delay, are unscored because the preceding state is unknown.
Unknown timing is a caution, not an Apply block.

## Search

The hybrid learner proposes thresholds from six-second energy histograms, then checks a
bounded shortlist against the recorded spans and the radar's timing. Search is
deterministic with fixed work limits, so it gives the same result on fast and slow
machines. It is heuristic: it does not prove a global optimum or that no better
configuration exists.

- **Move gates** are set by rule: one third of the way from their empty-room noise to the
  level real arrivals reach, rounded up. Empty-room noise is the 99th percentile of
  half-hour maxima over trusted empty time (human Empty labels, plus automatic Empty
  with two minutes trimmed from each edge, pieces under five minutes dropped and pieces
  whose still energy looks occupied set aside). Human Empty labels set a floor. A move
  gate that never rises above its noise at arrivals is turned off.
- **Still gates** are then searched on still energy alone, without being asked to catch
  presence only move gates see. A radar with only move gates enabled searches them.
- When both human classes separate, the preferred threshold is halfway between the
  highest empty energy and the lower quartile of presence energy. Human detections take
  priority over that margin. There is no fixed minimum threshold.
- The search also tries per-gate false-positive bounds, a quiet starting point, and
  pairs that raise a noisy gate while lowering a supporting one.
- A candidate may not make more empty periods mostly false-active than the starting
  settings (relaxed by presence priority above).

Every recorded reading is evidence: brief dips and spikes are not discarded as
outliers. Gates with no observations keep their current threshold.

### Chronological check

When labelled episodes allow, Learn first cuts the history between whole episodes near
80%, with both states on each side. It learns two candidates from the earlier part (one
where automatic evidence only breaks ties, one that also weighs quality-corrected
automatic evidence) and keeps the automatic evidence only if, on the later part, it
improves missed or false-active time without missing more presence, more empty time or
more empty-room episodes. The final result is learned from all retained evidence with
the chosen policy, and the check is reported. Without a valid cut, the corrected policy
is used and the result says so.

### Evidence limits

- At least five minutes of observed time in each state (shown as 50 six-second units),
  combining human and confident automatic evidence. There is no minimum human share.
- Automatic evidence from the last 28 days; human labels newest first up to 400
  labelled hours (keeping the label that crosses the cap whole).
- **Evidence budget:** a history of up to 50,000 recorded changes is used whole. A denser
  one is learned from representative periods: each class's newest time, quietest
  presence and loudest empty noise, then the minutes around label edges, kept whole;
  the rest is sampled evenly, each sample standing for the time it represents so totals
  stay unbiased. Episodes and guess quality are still counted from the whole history.
  The result says when the budget was used. If the device timing needs more context
  than the budget allows, the result is insufficient rather than partial.
- Readings are weighted by how long they held, not how many were recorded.

## Learning sets

Zones that share a radar form a learning set (its `set_id` is the sorted group ids
joined with `+`). Learn, Apply and overnight learning all work on whole sets; a shared
radar gets one value per gate. A miss counts only when no member detects after its own
timing; every member's false activity counts against the group. Equally scoring
settings favour thresholds inside supported gaps across all radars. There is no
strongest-radar winner, and a weak radar is not forced to cover every moment. Zones that
cannot be scored are skipped and reported.

Results appear in each member's User learnt / Autolearnt slot, marked as joint. Apply
on any member opens the group review and applies the whole recommendation. A joint
result whose companions changed must be relearned. Automatic Apply needs every member to
allow it and a lower joint cost on fresh evidence, and is refused if any zone was skipped
or would get worse.

## Assessment

**Assess current settings** replays 24 hours, 3 days or 7 days with each radar's own
timeout and delays, then combines detections: any member may detect, any member may
cause a false positive, and overlaps count once. Only shared recording coverage is
assessed; gaps and timing warm-up are excluded and reported. Unlike Learn, the replay
follows recordings across label changes, so a hold can run into an empty period. The
role report shows each radar's **Covers alone**, **False-active** and **Missed what it
saw**; **Radar on its own** is the separate per-radar learner score, not comparable with
the room figures. Results are stored with the scorer version and kept until membership,
labels, thresholds, timing or the scorer change.

## Radar timeout suggestion

After any automatic Apply of thresholds, Learn scores timeouts of 1, 2, 3, 5, 8, 12, 15,
20 and 30 seconds plus the radar's current one at the live thresholds: missed labelled
presence (and the dark spells it falls into), and false light and false starts in empty
time (human Empty labels of the radar, its zones and rooms, and its trusted Empty
guesses), priced like Learn. It suggests the shortest timeout keeping at least the
configured share (80% default, 50–100%) of the best one's saving over the shortest;
100% picks the cheapest. A saving under 3% of the shortest timeout's price is noise and
keeps the shortest. Missed presence is priced at the room's priority for room labels and
at the highest priority of the radar's rooms for its own labels.

In a room with several radars, labelled presence is missed only where none of them holds
presence. Each radar is scored with the others at their own timeouts, and a room Learn
tunes them one at a time until none changes. Automatic timeouts write the suggestion to
the radar's timeout number and check the radar reports it; results learned at the old
timeout become stale.

## Apply and automatic Apply

Apply needs a complete current-model result and a gate configuration matching the one
reviewed. It writes through the paced writer, serialised per device, lowering
thresholds before raising others so coverage is never withdrawn first, and checks the
reported values. It is not an atomic hardware transaction: a failure stops further
writes and leaves written values in place, and only a fully successful Apply rotates
Current/Previous. On the first Apply, the original thresholds become Previous; if
settings were changed outside the tuner, those live values become Previous instead.

Automatic Apply compares the new result with the **live thresholds** on one fresh
snapshot of the same evidence and timing, and writes only on a strictly lower unrounded
cost. Stored display scores are never used to authorise writes. Changes to labels,
timing, configuration, recording or the auto-apply setting during a write stop further
writes. If labels change during a scheduled Learn, it retries once on a fresh snapshot.

## Comparison scores

**Compare saved patterns** scores each distinct threshold configuration once:
`max(0, 100 − 5 × missed % − false-active %)`, using the same evidence (and budget) as
Learn. Insufficient evidence is unscored, never zero. Scores are kept with their scorer
version, reused across visits and restarts, and recalculated only when the scorer
changes; at most five configurations are kept per radar. New recordings or label
changes do not silently change a stored score, so scores from different dates are not a
controlled comparison. **Check stored scores** retries unscored or failed ones only.

## Reviewing conflicting labels

**Periods driving this result** lists the recorded chunks contributing most error time.
For an empty period it also shows the score without it **at the same thresholds** (not a
refit, and not proof the label is wrong). **Review periods that disagree with their
labels** lists up to 24 precise ranges and 24 session summaries with their triggering
gates. **Review on graph** opens the range in History; nothing is changed until you save.

A brief empty-room burst is not automatically invalid: a person, pet or other activity
may cause it. Keep dog-only periods labelled Empty when training for people. Deleting
valid difficult examples would only hide false positives. Zoom in on a period to see
its short-lived readings; wide windows average them away.

## Background jobs

Learn, assessments and reference rebuilds run as server-owned jobs: leaving the panel
does not stop them, repeated requests share one job, and the latest report per radar is
kept. A Home Assistant restart interrupts running jobs; they are reported as interrupted
and not resumed. Charts and recorded-data reads use their own low-priority worker so they
never wait behind Learn. Each worker runs one job at a time under memory, CPU-time and
wall-time limits.

## What the results do not prove

Scores are observed metrics on correlated samples, not proof of field accuracy. A
backtest from the same session is not an independent trial. Firmware hold behaviour,
installation, unsampled spikes, range resolution and custom filtering all affect real
detection. The radar receives static thresholds, not the estimator. Test quiet sitting,
different positions and empty sessions after applying.

The [autolabelling replay report](autolabelling-validation.md) records the gains,
regressions and limits of automatic labelling on the supplied recordings.

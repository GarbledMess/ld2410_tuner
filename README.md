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

## Repository layout

```text
custom_components/
  ld2410_tuner/
    __init__.py
    config_flow.py
    inference.py
    history.py
    learning.py
    manifest.json
    services.yaml
    brand/icon.png
    static/ld2410-tuner-panel.js
    translations/en.json
hacs.json
README.md
tests/
tools/validate_package.py
.github/workflows/validate.yml
```

Only the integration directory is installed by HACS. Repository metadata, tests
and CI stay outside it. The layout follows the
[HACS integration requirements](https://www.hacs.dev/docs/publish/integration/).

## Calibration workflow

1. Enable engineering mode and the per-gate energy and threshold entities on the
   LD2410. Entity IDs must retain the existing `*_g0_move_energy` /
   `*_g0_move_threshold` naming convention (gates 0–8, move/still).
2. Label the empty room NOT PRESENT with the normal fans, curtains and background
   activity. Use the timeout to avoid accidentally leaving a label running.
3. Label people PRESENT, including walking and sitting very still at each location
   that matters. Collect multiple sessions, not just a short walk past the sensor.
4. Select UNKNOWN between sessions to let automatic estimates contribute, or
   correct recorded periods using the chart. Human labels override guesses. A
   chart range explicitly marked UNKNOWN is excluded from both training sources.
5. Click **Learn thresholds**. Review device-wide validation in Details/Actions,
   then **Apply recommended thresholds** if the candidate passes. Test again in the
   actual room, especially quiet sitting and empty-room false triggers.

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
alone confirm presence. Missing readings produce UNKNOWN.

Before human references exist, it warms up a background estimate and starts making
tentative guesses. Its background stops adapting during likely occupancy so a
stationary person is less likely to become the baseline. Bootstrap confidence is
capped at 65%; partially human-guided estimates at 80%; fully guided estimates at
98%. These scores are **heuristic**, not empirically calibrated probabilities.
A room already occupied during startup can fool a background-only estimate.

Each stored observation retains its automatic label and confidence alongside the
gate energies. A guess contributes `0.20 × confidence` of a human sample: 80%
confidence gives weight 0.16. Where human examples exist, total inferred weight in
each class is capped at 25% of human weight (20% of the combined evidence), so
volume cannot overwhelm human labels. Scores below 55% are not used. Previously
stored history without per-sample confidence remains readable but is not assigned
invented confidence retroactively. Human corrections and explicit UNKNOWN ranges
always override the stored guesses. Feedback buttons adjust the estimator's bias;
use the chart/training labels to supply authoritative occupancy examples.

The threshold search starts each observed gate near its background level, rather
than disabling every gate at 100. It compares improvements across all gates, keeping
useful overlapping coverage. A threshold of 100 is still possible when the observed
background warrants suppression, but is no longer the default for redundant gates.

The search first satisfies the existing human-labelled presence, missed-episode,
consecutive-miss, false-positive and burst-rate limits, across both the full and
recent observations. It then minimizes missed presence and false positives within
those limits. It no longer demands a perfect sample score at the cost of making a
gate nearly always on: an isolated miss within the 99.9% target can be preferable
to thousands of false positives. Entire missed episodes and consecutive misses
remain unacceptable. Human targets and refinements always outrank inferred evidence.

If the background starting point fails, the search also tries thresholds bounded
by the per-gate false-positive limits and a quiet starting point. This lets several
cleaner gates replace a noisy detector. A bounded pair search can raise a noisy gate
and lower a supporting gate together, escaping single-gate search traps.
Confidence-weighted inferred observations then refine otherwise tied human results.
The weight cap limits their influence; it never rejects a recommendation based on
the proportion of automatic data. Human corrections and explicit UNKNOWN exclusions
continue to win over any stored estimate for the same period.

When enough human-labelled observations exist, the latest 20% of each class is used
for an earlier-data backtest. That evaluation excludes newer inferred observations
to avoid leaking holdout labels through the estimator. The final recommendation is
then refit using **all** eligible observations, including newer estimates. Its
training results and the earlier-data backtest are reported separately. A failed
backtest does not veto a final candidate that learned the newly observed pattern.

The final candidate is checked against available human labels, both overall and in
the recent labelled slice, targeting:

- At least **99.9% presence recall**.
- No completely missed presence episodes and no run longer than one missed sample.
- At most 0.5% false-positive samples and one false-trigger burst per observed hour.

Actual human-label conflicts still block Apply and report the measured reasons.
Automatic-only recommendations can be applied, are explicitly described as estimates,
and do not claim human-validated accuracy. Degenerate candidates that miss every
estimated presence observation or trigger on every estimated empty-room observation
are rejected when no human examples of that class are available.

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
- Apply requires the reviewed, passing recommendation and unchanged available
  threshold configuration. Writes are serialized per device, raise thresholds
  before lowering others, and stop/report partial failure. Concurrent Apply calls
  are rejected. Service completion is not independent hardware readback; after a
  partial failure, reread configuration and learn again before retrying.
- Websocket handlers use the current runtime after reload; snapshot access now
  matches the panel's administrator-only access.

## Verification

From the repository root, run backend/algorithm regressions (Python standard library; HA boundaries stubbed):

```sh
python3 tests/test_tuner.py
```

Run the real Chromium panel harness with Playwright installed externally:

```sh
PLAYWRIGHT_MODULE=/path/to/node_modules/playwright node tests/panel.cjs
```

The browser harness supplies simulated HA websocket responses and checks populated
charts, three-card collapse/expand and polling, draft retention, out-of-order
requests, errors/retry, drag/cancel, confidence displays, automatic-only recommendations, focused range
changes, fixed ending timestamps, cached windows, stable refresh geometry, mobile
overflow, and reconnect. It writes
screenshots to `/tmp/ld2410-desktop.png` and `/tmp/ld2410-mobile.png`. Set
`PANEL_SOURCE` to another panel file to run the same regression against it.
These checks do not exercise a deployed Home Assistant instance or physical radar.

GitHub Actions checks the local integration package, runs Hassfest, and runs the
Python and release regressions on pushes and pull requests. A successful push to
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
executor. Valid version-1 blocks become version 2 with unknown automatic labels
and confidence; confidence is never invented for older observations. Recorded
energies, timestamps, missing values, and human-label precedence are preserved.

Expired samples and labels are removed after the 30-day retention window. Corrupt
blocks and wholly unusable rows are removed; invalid individual gate readings
become missing. Duplicate timestamps keep the later stored record. Histograms are
rebuilt from retained labels plus the separate untimed legacy baseline. Untimed
legacy evidence is retained because its age cannot be inferred. A second cleanup
of unchanged data is idempotent. Concurrent sampling or corrections defer that
device's cleanup to the next pass. Removing or repairing observations invalidates
old recommendations; format-only normalization preserves current-model results.

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

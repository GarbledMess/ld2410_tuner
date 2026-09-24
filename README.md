# LD2410 Tuner

Home Assistant custom integration for labelled radar calibration and gate history.

## Installation

### HACS

In HACS, open **Custom repositories**, add
`https://github.com/GarbledMess/ld2410_tuner` with type **Integration**, then download
**LD2410 Tuner**. The GitHub repository must be public for HACS to access it.
Restart Home Assistant, then add **LD2410 Tuner** under **Settings → Devices &
services → Add integration**. Refresh the browser fully to load the panel.

### Manual

Copy only the repository's `custom_components/ld2410_tuner` directory into
`<Home Assistant config>/custom_components/ld2410_tuner`, then restart Home Assistant
and add the integration as above. Do not copy the repository root into that folder.
Existing stored training/history are preserved; earlier recommendations must be
learned again before Apply. Local brand icons are supported by Home Assistant 2026.3+.

## Repository layout

```text
custom_components/
  ld2410_tuner/
    __init__.py
    config_flow.py
    inference.py
    learning.py
    manifest.json
    services.yaml
    brand/icon.png
    static/ld2410-tuner-panel.js
    translations/en.json
hacs.json
README.md
tests/
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
5. Click **Learn safe thresholds**. Review device-wide validation in Details/Actions,
   then **Apply validated thresholds** if the candidate passes. Test again in the
   actual room, especially quiet sitting and empty-room false triggers.

The learner can propose provisional thresholds from automatic estimates before
human labels exist. Apply requires at least 50 complete human-labelled time
samples in each class for separate validation.
It retains up to 5,000 recent complete samples per class for fitting. History is
sampled at a minimum five-second spacing on a two-second timer (normally six
seconds), independently of whether values change. Unknown/unavailable gates are
missing observations, never zero or reused cached energies.

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

The threshold search first covers distinct human-labelled presence episodes, then
improves the weakest episode and total presence coverage. Confidence-weighted
inferred presence/absence helps choose between equal human-coverage candidates and
cover additional likely occupied locations. It uses a whole-device false-positive
budget; a two-point background margin is a tie preference, not a floor that must
reject faint stationary presence. Uninformative gates can be suppressed at 100.

The latest 20% of each human-labelled class is held out from fitting. Automatic
observations at or after the first held-out timestamp are deferred until later
human-labelled validation is available; this prevents predictions whose estimator
has seen holdout labels from leaking them back into fitting. Provisional proposals
can use newer guesses, but they cannot be applied without human validation.

Both human-labelled training and validation must meet:

- At least **99.9% presence recall** (small sets therefore permit zero misses).
- No completely missed presence episodes and no run longer than one missed sample.
- At most 0.5% false-positive samples and one false-trigger burst per observed hour.

Metrics expose missed samples, missed episodes, longest missed runs and false-trigger
bursts rather than hiding failures behind one accuracy percentage. Burst rates use
observed samples and approximately six-second spacing, not unobserved wall-clock
periods. Current thresholds are evaluated on the same holdout when available.

These are observed sample metrics, **not proof of near-perfect field accuracy**.
Adjacent samples are correlated; a holdout from the same session is not an
independent room trial. Firmware occupancy hold time, installation, unsampled
spikes, range resolution, and custom sensor filtering can affect actual detection.
The optimizer is greedy, so a blocked candidate does not prove no configuration
could work. Quiet-presence and different-position sessions remain essential.
The richer estimator guides training; the physical radar still receives static
per-gate thresholds, not the Bayesian model itself.

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
- Chart fetches have a timeout, ignore obsolete responses, refresh periodically,
  and expose failures with a Refresh retry. Current buffered history is visible.
- History/timeout drafts, collapse state and chart selections survive polling.
  Pointer cancellation, component disconnect and reconnect clean up correctly.
- Uniform time sampling includes stable empty-room and quiet-presence readings.
  Automatic predictions have explicit confidence and bounded training weight.
- Retrospective corrections are idempotent, include buffered/live intervals, and
  use half-open ranges. Empty legacy snapshots no longer double-count early data.
- Earlier recommendations must be learned again with the temporal model and
  stricter acceptance targets. Clear stops training and invalidates learned values. Expired sessions end at the
  actual deadline; cancelling an old timeout cannot remove a replacement timer.
- Unload flushes partial history and cancels the pending save. Long gaps start a
  new history block instead of overflowing timestamp offsets. An abrupt process
  failure can still lose the current in-memory history block (up to 60 samples).
- Chart aggregation uses bounded buckets. Chart/history reads and calibration run
  on detached snapshots in HA's executor; snapshot polling no longer schedules
  unnecessary storage writes. Retrospective histogram rebuilding remains on the
  event loop and may be noticeable with a large 30-day history.
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
requests, errors/retry, drag/cancel, confidence displays, provisional application
blocking, mobile overflow, and reconnect. It writes
screenshots to `/tmp/ld2410-desktop.png` and `/tmp/ld2410-mobile.png`. Set
`PANEL_SOURCE` to another panel file to run the same regression against it.
These checks do not exercise a deployed Home Assistant instance or physical radar.

GitHub Actions runs HACS and Hassfest validation plus the Python regressions on
pushes and pull requests. Hosted validation runs after these files are pushed;
local regression success does not establish HACS store inclusion or live HA compatibility.
The license is currently undecided, so the HACS license validation check is expected
to fail until an appropriate open-source license is selected. GitHub repository
visibility, description, topics and issue tracking also need to satisfy HACS checks.

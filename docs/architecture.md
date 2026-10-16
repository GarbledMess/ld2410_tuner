# Code ownership and invariants

The Home Assistant adapter owns lifecycle and external calls. Pure numerical code
has no Home Assistant dependency. Runtime operations receive one explicit
`TunerRuntime` argument; its class exposes a stable API by binding named functions
from the feature modules. There is no mixin hierarchy or import-time registration.

```text
custom_components/ld2410_tuner/
├── __init__.py              # Home Assistant lifecycle
├── config_flow.py           # Required Home Assistant entrypoint
├── const.py                 # Integration-wide constants
├── runtime/                 # Coordinator, discovery, websocket API
├── presence/                # Automatic labels and pure inference
├── rooms/                   # Areas, zones, room labels, attribution, learning sets
├── history/                 # Recording, cleanup, human labels/timeouts
├── calibration/             # Explicit Learn/Apply, fitting, search, metrics
├── presentation/            # History queries, dashboard snapshots/exports
├── static/
│   ├── ld2410-tuner-panel.js # Public panel entrypoint
│   └── panel/               # Card, controls, learning summaries, chart,
│                            # SVG drawing, selection, shared constants/styles
├── brand/
└── translations/
```

| Package | Modules and responsibility |
| --- | --- |
| `runtime/` | `coordinator.py` owns state/tasks; `discovery.py` follows registry changes; `websocket.py` exposes administrator commands |
| `presence/` | `autolabelling.py` records guesses/feedback; `inference.py` implements the pure temporal estimator; `reference_training.py` builds per-radar references; `rebuilds.py` owns reference rebuild policy: events only `mark_stale` (`reference_stale_since`), and `refresh` (overnight pass, "Refresh references") rebuilds in the executor (coalesced, revision-checked) |
| `history/` | Recorded history is owned by the day store and nothing else holds it. `daystore.py` owns the folder `<config>/.storage/ld2410_tuner/history/<radar>/<YYYY-MM-DD>.log` (live) and `.day` (finished; UTC days): `append` on the event loop touches memory only, `flush` (executor) appends logs and finishes ended days, `open` repairs an interrupted finish and ends coverage left open by a crash; `dayfile.py` is the one codec (append-only log records; zlib columnar finished day, one byte a gate per change, 255 missing, float32 for a non-integer day, plus the day's guess runs); `ingest.py` is the one writer (delivered changes at their own millisecond time, guess run changes, explicit ends of coverage); `live.py` is the poll's recording work (only values events missed); `spans.py` is the one reader (`events`, `day_spans`: physical spans, a reading holds until the next); `day_results.py` keeps what learning derives from a finished day (row counts, evidence chunks, quality histograms, guess runs) until that day file or the labels inside it change (`DayStore.identity`, `day_results.fingerprint`), so daily learning recomputes only new or relabelled days; `summaries.py` and `summary_cache.py` are label-independent duration indexes per finished day (optional `.sum` files, worker-only, 64 MiB, never own history); `guesses.py` and `trust.py` read automatic guess runs as evidence; `label_lookup.py` and `labels.py` own human labels/timeouts and `label_counts.py` the per-radar labelled time behind histograms; `convert.py` is the only reader of the old JSON formats (one-way, at startup); `cleanup.py` cuts time ranges out of finished days; `policy.py` owns retention settings; `budget.py` plans age/size cuts; `storage.py` owns guarded persistence and recording controls; `recording.py` owns runtime history operations |
| `calibration/` | `service.py` owns Learn/Apply and hardware guards; `jobs.py` owns task lifetime, result saving and job reports; `results.py` owns the four saved-result slots; `event_fitting.py` (with `event_replay.py`, `event_timing.py`, `event_quality.py`, `event_preferences.py`), `metrics.py`, `scoring.py`, `constants.py` own the numerical learner over physical spans; `evidence_budget.py` chooses a bounded, representative set of recorded time when a history is too dense to replay in full; `duration.py` scores observed elapsed time; `influence.py` attributes errors to recorded chunks; `recovery.py` owns bounded pre-Learn recovery; `timing.py` and `timing_metrics.py` replay sampled device timing, while `timing_config.py` reads optional HA timing entities |
| `rooms/` | `layout.py` builds the area/zone/elsewhere/area-less/unassigned tree; `room_labels.py` owns manual and sensor room labels (manual-only revision for stale edits, separate sensor revision; finalising a sensor label marks member references stale); `room_sources.py` reads camera/boolean/Bermuda sources; `truth.py` applies precedence (own human label, implied, member-derived human truth, room sensor labels, member guesses, then the noisy-OR estimate; sensor labels below `MIN_AUTO_CONFIDENCE` are ignored); `attribution.py` decides which radar saw a room's occupancy (own human label, else sustained energy above its empty baseline) computed once per job (`event_attribution`); `seen_by.py` uses that same function and cache for chips; `roles.py` computes per-radar role metrics, and `objective.py` per-radar preferences, from it too (a job computes it once per radar); `sets.py`, `objective.py`, `fitting.py`, `learning.py`, `application.py` implement linked learning sets, set-level jobs and Apply |
| `presentation/` | `charts.py` aggregates history; `snapshots.py` builds dashboard/export data |
| `static/panel/` | `view.js` owns the shell/draw lifecycle; `card.js`, `controls.js`, `learning.js` own card sections and interactions; `chart.js`, `visualization.js`, `selection.js` own history requests, SVG drawing, and range interaction; `jobs.js` renders server-owned learning progress; `layout.js` renders the room tree and group headers; `room_timeline.js` and `room_report.js` render room labelling, Seen-by chips and the role scoreboard; `constants.js` owns shared values |

Package initializers only document ownership; imports name concrete modules. The
release allowlist includes each initializer and nested asset explicitly. Tests and
replay tools use the same grouped module paths as the installed integration.

## Runtime contracts

- Human labels and explicit UNKNOWN exclusions override automatic guesses. Guesses
  never enter the authoritative human reference histograms.
- Learn and Apply are separate operations. `runtime/schedule.py` may run Learn on
  the configured daily schedule; only an explicit Apply writes gate thresholds.
  Saved results are bounded to manual, overnight, current and previous slots.
  Applying a selected slot pins its result ID and reviewed live configuration;
  partial writes do not rotate current/previous results.
- Learning job ownership is independent of websocket callers. `start_learning`
  acknowledges acceptance immediately; the legacy awaited Learn command and nightly
  scheduler join the same per-device task. That task saves each requested slot before
  completion, even when every caller disconnects. Snapshots expose one bounded job
  report per device. Errors and shutdown interruption are recorded; runtime startup
  marks any persisted running report interrupted. Job status updates do not redraw
  focused inputs. Stage progress is indeterminate, not a fabricated completion rate.
- The fitting search retains its iteration order and strict tie-breaking. The
  search minimizes a finite time cost (5 times missed occupied-time rate plus
  false-active empty-time rate), then uses confidence-weighted automatic time.
  The 99.9% recall target assesses quality rather than granting a free miss budget. Quality failures are
  advisory for manual Apply; entity identity and write guards still apply.
- Timing replay is an offline calibration input, never a replacement presence
  entity. It combines gates before the radar hold and any known on/off filters.
  Search, recent validation and review periods use the same replay; raw metrics
  remain separate. Unknown timing never acquires guessed defaults, and Apply
  never writes timing controls. Timing changes during Learn discard that result.
- Learn recovery shares the device-operation guard with Apply. It may query or
  restart a radar with missing configuration, but never applies thresholds.
  Bluetooth restoration is persisted, and recordings pause during recovery.
- Recording opt-in is per device; retention and size settings are global. Discovery
  defaults new devices to paused. The sampler updates live readings but must not
  change training evidence while paused. Disabling closes the live label.
- All runtime writes go through `async_save`, which first flushes the day store, then
  serializes an isolated snapshot, enforces the total ceiling (history folder plus the
  JSON envelope), then delegates to HA Store. TunerStore selects the standard JSON encoder
  so byte accounting uses the same indented envelope. The JSON store holds no history.
- History is change-only and time-weighted. Every delivered gate change is stored at its
  own millisecond time; a reading holds until the next one, and coverage ends only on an
  explicit event (pause, removal, blocked storage, recovery, stop; after a crash, the last
  stored record). Every consumer weighs a reading by how long it held, never by how often
  it was recorded. Automatic guesses are runs `(start, end, state, level, basis)` stored in
  the same day files (`ingest.guess`, recorded every `HISTORY_SAMPLE_INTERVAL`). Every
  consumer of evidence reads `guesses.evidence` (`history/trust.py`, `constants.TRUST_*`:
  calibrated confidence, present runs cut where every gate reads within the ceiling of the
  radar's Empty-labelled histograms). A room is present on automatic evidence only when
  `AGREEING_MEMBERS` members say so (`evidence.agreed`, `truth.estimate`).
- The one-time conversion of JSON-held history (`history/convert.run`) runs in the executor
  after setup returns, before recording or panel registration; the JSON is then saved without it. It is
  one-way; re-running after an interruption rewrites the same day files.
  The startup task is tracked separately until the runtime is published; unload stops its
  work generation and cancels it without persisting partially prepared state. Initialization
  failures are logged with a reload instruction.
- Expensive work uses `history/pace.run`: admission waits asynchronously, so queued jobs
  do not occupy Home Assistant's shared executor. The `background` decorator executes on
  one integration-owned worker, where OS priority can be lowered without affecting an HA
  worker reused later. Cooperative checkpoints target 20% CPU, with a 5 ms work slice;
  this is a target, not a hard CPU or memory limit. Conversion, reference imports and
  rebuilds, recounts, save preparation, learning, comparisons, charts and recorded-history
  exports share this lane. Unload's final small state save bypasses the lane so it cannot
  wait behind abandoned work. Cancelling a caller keeps admission occupied until its worker
  actually finishes; unloading invalidates queued jobs and stops active work at its next
  checkpoint. Short log flushes remain separate. Periodic sampling yields between radars,
  skips overlapping ticks and stops between radars on unload. Registry access and live
  state changes remain on HA's event loop. Seen-by attribution uses the latest 72 hours;
  this display window does not remove stored history or change learning evidence.
- Learning replays physical spans exactly. When a radar's recorded rows exceed the learning
  budget, `calibration/evidence_budget.select` keeps the most valuable recorded time whole
  (newest per class, quietest presence, loudest empty noise, then stretches beside label
  edges) and samples the rest of each class evenly, each sampled window standing for its
  class's unsampled time (`Window.scale`), so class totals and rates stay unbiased.
- A radar's readings are classified when something notably changes, not on every tick
  (`inference.reclassify`: a channel `CHANGE` away for `SUSTAIN` seconds, a changed filter,
  references, calibration or external source, a classification not at rest, or the
  `HEARTBEAT`); otherwise the last result stands (`autolabelling._reuse`), current for the
  panel and the feedback gate, and the temporal filter and confirmation count the time as
  seen (`inference.hold`). The views of one job share the members' guesses
  (`attribution.member_guess`).
  [HA's Store envelope](https://github.com/home-assistant/core/blob/dev/homeassistant/helpers/storage.py)
  and [JSON serialization](https://github.com/home-assistant/core/blob/dev/homeassistant/helpers/json.py)
  define that boundary. Pending buffers are not counted as persisted bytes.
- Budget planning is pure and tested without modifying real recordings. It cuts time
  ranges out of finished days only (the live day is immutable to maintenance). Human labels
  take precedence over automatic confidence; size eviction drops automatic time globally
  first (lower confidence, then older), then the oldest human-labelled time. Optional
  summary files are evicted before any recorded history. Labels older than the human
  retention expire after planning. Settings and saved recommendations are protected. Revision/pending-data guards reject stale
  cleanup. A write that cannot fit is refused, not silently allowed over the cap.
- Missing energy is not zero (255 in a byte column, NaN in a float column). Converted
  observations without confidence do not acquire invented automatic labels.
- Executor work uses detached views. Revision guards prevent stale work from
  overwriting newer sampling or human corrections.
- History windows share a fixed ending timestamp until Refresh. Obsolete requests
  cannot overwrite the current chart; failed refreshes retain the last plot.
- Static modules ship together in the explicit release allowlist. The versioned
  panel entrypoint and uncached static responses require a full panel reload after
  an update.
- Room-first data is additive: `room_labels`, `room_label_revision`,
  `room_sensor_revision` and `room_presence_sources` are new stored keys.
  `room_learning` is keyed by `set_id`; a one-group set_id equals its group_id, so
  older entries remain valid.
- Human evidence trains at weight 1. All automatic evidence (a radar's own guesses,
  room estimate, sensor room labels, rule-2 attribution) counts at confidence times the
  integration-wide "Automatic evidence weight" (`calibration/settings.py`, stored in
  `learning_schedule`, 0-1, default 0). Both learners rank on human cost +
  `scoring.automatic_share` x automatic cost (share capped at `AUTO_CLASS_CAP` of the
  human mass), then the earlier lexicographic tie-breaks; at 0 automatic evidence only
  breaks ties. References are tagged human, entity or radar.
- Automatic Apply of a learning set refuses when a zone was skipped or would get
  worse.
- Room sensor activity (`room_sensor_revision`) makes assessments stale only. Like
  device-level sources, a sensor-driven retrain marks references stale without bumping
  member `label_revision`, so learn results, Apply and open label edits stay valid.
  Manual room label edits still invalidate member learning.
- A reference rebuild reads the radar's whole history (and, for a grouped radar, room
  evidence), so events never run it: edits update revisions at once and mark the
  references stale (`reference_stale_since`). `presence/rebuilds.refresh` rebuilds
  stale radars once a day at the schedule time (at the start of the overnight pass,
  or on their own under a separate `references_day` claim when learning is off), or
  on the panel's "Refresh references" (`refresh_references`), in the executor. A result installs only if the
  radar's label revision and related manual room label revisions are unchanged; a
  failed, discarded or cancelled pass keeps the radar stale. Learn does not read
  references, so it does not refresh them first.

## Weak-machine efficiency

The integration is meant to run on a Raspberry Pi class machine, so work that scales
with history or with the registry is kept small. Each fast path has a test that checks
it against the plain computation it replaced (`tests/old_replay.py`,
`tests/old_background.py`), and the benchmark (`.superpowers/sdd/.../eval/perf_bench.py`)
keeps the numbers.

- `entity_index.py` reads the entity registry once (by device, with the radars among
  its entities) and again only when its size changes or `handle_registry_update`
  hears of a change. Discovery, the dashboard thresholds, device timing and the
  threshold configuration ask it instead of walking the registry.
- The room estimate remembers each member's newest sample and its recent guess runs
  while the member records nothing new (`room_labels._recent`), so a poll that finds one
  new sample reads that sample, not the last hour.
- Day files decode to columns (`array` times and ends, a `bytes` column a gate), so a
  radar-day of 777,600 changes decodes in well under a second; room evidence rows keep
  the same byte columns (`rooms/evidence.Rows`).
- Learning keeps per-day derived results (`history/day_results.py`, memory only, bounded by
  entries and 64 MiB) keyed by the finished day's identity and the labels inside it, so a
  repeated Learn (the next overnight run) re-derives only days that are new or relabelled;
  live and closing days are never kept.
- Label counts, label edits and reference rebuilds read per-day duration indexes
  (`history/summaries.py`) instead of replaying every change; a label edit recounts only
  the time whose label changed (`label_counts.update`).
- The timing windows return spans as `(starts, ends)` lists (`timing.merged`), runs of
  hits come from one regular expression (`timing.hit_runs`), and the joint search keeps
  each window's spans for the hits it has seen (`replay.Projector`, bounded) and the
  rates of each set of hit masks it has ranked (`Objective.rates`). `evidence.Readings`
  keeps a sample's energies a byte each.

## Quality checks

`tools/check_complexity.py` checks every function in the integration and Python
tools, including nested functions, against a ceiling of 10. Ruff enforces a common
format and catches unused code, import errors, and unsafe Python patterns. Pytest
coverage includes all integration and tool files and fails below 85%. Playwright
loads the real ES modules against a simulated Home Assistant websocket boundary.

Exact before/after comparisons covered 6,000 estimator observations, 24 varied
threshold-fitting fixtures, history cleanup across ten stored devices at two
retention windows, and all 15 replay reports. Private recordings and saved baseline
code are not part of the repository. Synthetic regression tests remain in `tests/`.
These checks do not substitute for a live Home Assistant and hardware trial.

## Verified local result

| Measure | Before | After |
| --- | ---: | ---: |
| Integration entrypoint lines | 1,436 | 135 |
| Maximum production Python cognitive complexity | 143 | 10 |
| Regression tests | 83 | 104 |
| Full integration and Python tools line coverage | 64% | 94.65% |

All 201 production Python functions meet the complexity ceiling. Ruff lint and
format checks, Prettier, and the Chromium regression harness pass. The deterministic
1.9.3 archive contains all 36 runtime assets and excludes development dependencies,
tests and recordings. CI is configured to repeat the checks before version-triggered
releases; a hosted CI run and a live hardware trial have not been performed locally.

## Sonar cleanup

Cancellation now propagates after timeout cleanup, so cancelled tasks remain
observably cancelled. Device iteration deliberately uses a shallow snapshot across
executor awaits; registry discovery cannot invalidate its iterator. Synchronous
state updates use Home Assistant's callback decorator, and redundant setup hooks
and unused internal arguments have been removed.

The card renderer is split into focused renderers and event wiring; SVG drawing is
separate from chart fetching and refresh state. Nested conditional/template
expressions were replaced with named intermediate values or helpers. The browser
regressions exercise the actual nested ES imports and retained DOM controls.

The local SonarQube for IDE extension (5.10.0) analyzed all 41 Python/JavaScript
source and test files with its installed Python 5.31.0 and JavaScript 13.9.0
analyzers. The default active rules reported 61 findings before cleanup and zero
issues or security hotspots afterwards. No `NOSONAR` markers, disabled rules, or
source exclusions were added. This is local extension evidence, not a hosted
SonarQube server quality-gate result.

### Configurable automatic application

`calibration/automatic.py` compares live and newly learned thresholds on a common
fresh history snapshot through the existing scorer. Persisted display scores never
authorize writes. The global auto-apply scope defaults to overnight only; selecting
every completed Learn also enables the same path from manual jobs, independently
of the overnight schedule. Shared callers reuse the job decision, including callers
joining during application, rather than comparing and writing the same fit twice.
A strictly lower weighted duration error enables the normal guarded, paced writer.
The device operation lock and repeated state guards stop conflicting writes;
partial application is reported separately and does not promote the saved Current
slot. Bounded job and overnight reports contain the comparison, scorer version,
reason and write outcome. The global schedule owns the auto-apply switch (on by
default when overnight learning is enabled).

The global automatic Apply settings are defaults. `automatic.policy` resolves the
optional device `auto_apply_override` (`off`, `overnight`, `all`); an absent value
means inherit, and choosing inherit removes the override. All job eligibility and
in-flight write guards use this same resolver. Explicit overrides take precedence
over the global default, while the global overnight schedule and recording guards
still apply. Snapshots and exports expose the override, global default and effective
mode; device controls use the admin-only `configure_device_auto_apply` route.

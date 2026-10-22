# Release notes

## 1.28.0 - 2026-10-22

- A room or zone with exactly one radar now trains that radar with its manual
  labels; the radar's own labels still win where both exist, and **Seen by** keeps
  showing what the radar itself saw. That radar is labelled on its room's timeline,
  which lists any labels of its own and can open its editor.
- Hide a zone that has exactly its room's radars and nothing of its own; the room
  names it, offers **Show**, and carries its Learn controls.
- Presence sources live on rooms and zones. A radar keeps its own sources form only
  outside every group or while it still has sources to move.
- Remove the History month calendar; choose days with the day picker.
- Hide empty results (score headlines, Radar on its own, snapshot outcomes, pattern
  comparison). Recommendations keeps the result picker, Learn/Apply and the outcome,
  with comparison, automatic Apply, timing and gate thresholds in one **Details**.
- Shorten help text to a line or two; the README keeps the detail.
- Disabled controls now say why next to them. Selection grips sit exactly on their
  boundaries at the ends of the day.

Validation: 2,421 workspace tests passed with 96.96% coverage. Desktop/mobile browser
checks passed locally and in the one-CPU Linux HA lab with real startup. With every
section open, the sample panel is 8,737 px tall instead of 14,102 px.

## 1.27.0 - 2026-10-22

- Load graphs in seconds instead of minutes. Graphs and recorded-data reads now run
  in their own low-priority worker, so they no longer wait behind Learn, reference
  or seen-by jobs. Each worker still runs one job at a time under CPU limits; the
  display worker has a smaller 256 MiB memory limit.
- Calculate charts from each gate's stored values directly instead of rebuilding
  every reading, about five times less work per radar-day with identical results.
- Redraw the room graph like the radar graph: every gate of the selected kind for
  each radar on one chart, with line style identifying the radar. G0–G8 buttons
  highlight a gate together with each radar's current threshold. The previous graph
  plotted each radar's strongest gate regardless of thresholds, so gates the radar
  ignores could dominate it.
- Add a **Recorded data** card showing the exact stored readings for each radar and
  day: millisecond UTC times, durations and gate values without averaging or
  rounding, actual file sizes and retained recovery files. Download a complete day
  as CSV with progress and cancellation; no partial file is saved.

Validation: 2,410 workspace tests passed with 96.95% coverage. Desktop/mobile
browser checks passed in the one-CPU Linux HA lab. A room graph over a dense
150,000-row day loaded in 2.4 s while a Learn job ran, with HA HTTP p95 of
24.5 ms. A complete 149,532-row supplied day exported with every value matching
storage exactly.

## 1.26.4 - 2026-10-22

- Preserve the last observed coverage of unchanged signals when flushing, so stable
  zero or 100 readings survive an abrupt restart. Persist unavailable transitions.
- Calculate room signals from simultaneous physical readings before downsampling.
  Preserve true step boundaries, outages, duration-weighted means and peak intervals.
- Share the radar graph's average line and recorded-range shading with room graphs.
  Show paused recording beside the graph. Keep successful radar results visible when
  another fails, publish completed results promptly, and retry only failed requests.
- Capture only the requested portion of mutable chart days. Return one compact
  room signal per radar rather than all gate series. Prioritise visible interactions
  while deferring automatic work in closed sections.
- Use lossless delta timestamps and coverage durations in finished day files, retaining
  readers for the original format. Existing files compact automatically in the protected
  startup transaction, one file at a time; every recorded change is retained.
  Older releases cannot read the new encoding; downgrades require restoring the
  corresponding history backup.
- Use two room columns when the panel has sufficient desktop width, retain a single
  mobile column and keep room, zone and radar cards within their owning cards.


Validation: 2,325 workspace tests passed, followed by 48 focused storage checks
including three additional malformed-file cases; combined coverage was 97.07%.
Desktop/mobile browser checks and real HA state-event acquisition through flush,
restart and chart retrieval passed in the one-CPU, 1,536 MiB lab. The protected
150,000-row workload retained every row; HA HTTP p95 was 21.650 ms (maximum
237.151 ms). All 122 supplied finished days round-tripped exactly, shrinking
from 24,287,325 to 14,621,437 bytes without removing any recorded change.

## 1.26.3 - 2026-10-21

- Fix normal multi-gate charts failing the worker result limit. Use bounded,
  compressed chart transport while preserving the full signal detail and gaps.
- Capture only the requested chart days instead of all retained history.
- Keep slow room graph requests alive across polling. Failed requests wait for an
  explicit refresh, retain any previous graph, and no longer report missing history.
- Populate room presence time fields by default and reset them when changing days.
- Replace floating History and Recommendations shortcuts with collapsible section
  headers beside their content. Replace the detached device triangle with a labelled
  disclosure immediately above the details it opens.
- Reduce oversized text and prevent wide graphs from enlarging their axis labels.
  Keep the room, zone and radar card hierarchy on desktop and mobile.

Validation: the full workspace Python suite passed 2,283 tests, followed by the
26 focused chart tests including two added transport boundary tests; combined
coverage was 97.05%. Desktop/mobile browser regressions passed in the constrained
Linux HA lab (one CPU, 1,536 MiB). A protected chart worker returned all 18 gates
and 3,636 points during the production job probe; HA HTTP p95 was 15.477 ms and
maximum 47.145 ms. Formatting, complexity, package, Bandit and offline workflow
security checks passed.

## 1.26.2 - 2026-10-21

- Stop rejecting valid history because of its size. Learning, references, charts and
  Seen-by no longer refuse a day above 131,072 rows or 4,096 guess runs, more than
  16 radars or 512 days, or more than 512 MiB of history files. Each decoded day is
  instead bounded by its actual memory (128 MiB) inside the unchanged mandatory limits.
- The resident day cache evicts or bypasses an oversized day instead of failing a job.
  Job day metadata is spooled to a private on-disk index rather than held in memory.
- Manual learning and other interactive jobs wait for a worker turn instead of
  failing when the worker is busy.
- Flushes write bounded prefixes of a large backlog and keep the rest pending for
  the next turn, preserving Clear generations and publication rollback.
- Storage transactions no longer refuse histories above 256 MiB or 8,192 files.
  Finished day and summary files are hard-linked into the private clone (with a
  copy fallback); only live logs are copied, and the refusal is now free disk space.
- Remove repeated characterisation runs and an obsolete source-text test.

Validation: the released suite passed 2,230 tests with 97.07% coverage; the workspace
suite passed with 97.06% coverage in constrained Home Assistant (one CPU, 1,536 MiB). Startup recovered 220,000 rows and 44,000 guess runs exactly
(HA p95 12.491 ms); the production worker probe answered 232 requests (p95 15.314 ms);
a protected child learned 86,400 rows in 5.071 CPU seconds. Browser regressions passed.
Jobs that exceed the 120-second CPU limit still fail; resumable work is not yet added.

## 1.26.1 - 2026-10-21

- Recover dense current-day history without rejecting normal per-second automatic
  guesses or multiple changes per second at startup.
- Apply coherent aggregate raw-buffer and estimated restored-memory budgets to
  startup, day rollover and storage maintenance. Keep the flush JSON limit separate.
- Allow storage children to write raw handoff files up to 64 MiB; retain the
  512 MiB address-space ceiling, 120-second CPU ceiling and lowest scheduling priority.
  Numerical children retain their 16 MiB file ceiling.
- Report actual counts and limits when a startup handoff cannot fit its budgets.

Validation: the full workspace Python suite passed with 97.07% coverage in constrained Home
Assistant. A real protected worker recovered 220,000 rows and 44,000 automatic
guess runs, then preserved the history exactly through append, flush and another
restart. HA answered 386 requests during this probe (p95 11.673 ms, maximum
32.072 ms); child peak RSS was 144,072 KiB. Further numerical optimizations remain
investigation-only.

## 1.26.0 - 2026-10-20

- Start every collapsible room, zone, device section and nested settings/help panel
  closed, with a short contents description on its heading. User choices persist
  during snapshot refreshes.
- Keep rooms, their zones and their radars inside nested cards, with consistent
  controls and layouts for desktop and narrow mobile screens.
- Add room signal graphs beside presence labelling, with day and signal controls,
  range selection and direct links to each radar's history.
- Put room assessment and configuration behind a labelled disclosure; add radar
  shortcuts for history and recommendations and reduce repeated explanations.
- Size individual radar plots, axis labels and touch-selection coordinates for
  each card's available width, including nested cards on mobile screens.
- Give live interaction the next available worker turn ahead of background jobs.
  Background learning, attribution and periodic storage work wait and resume;
  active operations keep ownership until their cleanup completes.
- Bound and coalesce chart requests, wait for worker admission, retry transient
  chart saturation and return useful WebSocket errors when admission is unavailable.
- Retain subprocess resource limits and low priority; numerical work stays outside
  Home Assistant's event loop.

Validation: the full Python suite passed with 97.06% coverage in constrained Home
Assistant. Production worker/recovery and dense legacy-history probes passed.
Full Linux browser regressions passed, including populated room graphs and
room/zone/radar containment at 320, 390, 768 and 1440 pixels.

## 1.25.1 - 2026-10-20

Fix regressions observed after the worker-isolation update.

- Remove history-directory scans from Home Assistant snapshots using a date
  inventory maintained by startup and storage publication.
- Accept dense legacy float history with bounded per-day decoding and a resident
  LRU, instead of treating all retained days as simultaneous memory usage.
- Restore derived-result reuse within each worker and account for physical day
  arrays retained by the summary cache.
- Save learned tuples and numeric mapping keys using HA-compatible JSON values,
  retaining strict worker protocol validation and source budgets.
- Wait for bounded worker admission for background Seen-by/reference refreshes,
  avoiding empty cached attribution caused by queue saturation.
- Dispatch scheduled learning only within 15 minutes of its configured start;
  restarting or enabling it later does not trigger a missed overnight run.
- Report CPU-budget exhaustion before the existing 120-second hard ceiling.
  The 512-MiB address-space limit and low process priority remain in place.
- Include parent-observed CPU/memory, wall time, shared OOM-counter changes,
  installed limits, operation/input counts and child milestones in worker failure
  diagnostics, including abrupt SIGKILL exits.

Validation: 2,204 tests passed with 97.04% coverage in constrained Home Assistant;
full browser regressions, dense legacy learning and live HTTP probes passed.

## 1.25.0 - 2026-10-20

Substantial performance and safety update. The existing HACS installation and
configuration remain supported; no separate app or manual dependency is needed.

- Run numerical learning, comparisons, room analysis, reference rebuilding,
  history recounts, charts and exports in bounded, low-priority subprocesses.
- Isolate heavy storage recovery, conversion, encoding, compression and trimming.
  Move commit and Clear filesystem work off Home Assistant's event loop.
- Preserve recordings and reject stale work across concurrent edits, cancellation,
  Clear, shutdown and worker failures. Retain pending saves when workers are busy.
- Report and record whole-percentage gate energies for package and non-package
  devices, while retaining compatibility with older fractional recordings.
- Reduce fitting overhead with direct byte columns, indexed collection and bounded
  cache retention. No NumPy dependency is added.

Workers require the mandatory Linux resource controls available in the tested
Home Assistant Container environment. Unsupported controls and oversized jobs
are refused rather than silently executed inside Home Assistant.

Validation: 2,155 tests passed with 97.02% coverage in constrained Home Assistant;
browser regressions and real learning/storage responsiveness checks passed.
The test VM uses Alpine Linux and official Home Assistant Container, not HAOS
or ARM emulation. Further timestamp, batching and compiled-kernel experiments
belong to the next iteration.

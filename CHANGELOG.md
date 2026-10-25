# Release notes

## 1.31.1 - unreleased

- Recorded history is far harder to lose when saving stalls. On one install a save never
  finished, leaving five hours of readings in memory only, and the restart for an update
  lost them, with nothing in the log. Saves of recorded history can no longer be held back
  indefinitely by other work queued for the worker: once one has waited a minute, it runs
  as soon as the current job ends. At shutdown, readings still in memory are written
  straight to disk if the normal final save fails or takes over a minute.
- If recorded history still goes unsaved for 15 minutes, the panel shows a warning, and the
  log says so once with details of what saving is waiting for (and again when it resumes).

## 1.31.0 - 2026-10-25

- Much less work on Home Assistant's main loop and in the background, with exactly the
  same results. Measured on a real install (desktop; a Raspberry Pi takes longer, in
  proportion):
  - Refreshing which radars see each room ("Seen by") takes about 2 s instead of 30 s
    once warm (about 9 s the first time).
  - The daily cleanup takes about 20 s instead of 64 s.
  - A room assessment takes about 27 s instead of 80 s, and a room Learn about 420 s
    instead of 590 s.
  - A radar's Learn takes about a fifth less processor time: the threshold search skips
    windows with no labelled time, which cannot change a score.
  - Saving recorded history to disk holds the history store's lock only while taking
    the records, and encodes each record once (about 30% less work per save).
  - Learning references from live readings no longer rebuilds the radar's label index
    every minute; it is kept until the radar's labels change.
  - A save holds the main loop for about 37 ms instead of 267 ms.
  - Startup replays the day's log about a third faster.
  - Rebuilding references, sampling, charts, snapshots and comparisons each take
    roughly a quarter to two thirds less processor time.
- Room attribution is now worked out a day at a time, and finished days are kept beside
  the history (within the existing result budget), so later runs only redo the days
  that changed.
- The JSON export now downloads as a file instead of being shown in the panel. The
  download link is signed, admin-only and expires after 10 minutes.
- Recorded data now sits inside each radar's card instead of at the top of the panel.
- All Bermuda settings are now in one Bermuda section. For each tracked device, tick
  the radars, rooms and zones it should label, optionally with a range and an area
  other than the target's own (a zone's own area is any of its radars' areas). A
  room's Bermuda labels still use that room's presence-source confidence, absence and
  trim settings. Existing Bermuda sources in radar and room settings move into the
  section once, keeping their ranges and areas, and the radar and room source editors
  now offer only Boolean sources.
- Unloading the integration can no longer save after Home Assistant has shut it down.

## 1.30.1 - 2026-10-24

- Labelling history no longer freezes Home Assistant. Adding or editing a history period,
  changing the presence label or a label timing out recounted each day the label touched
  on Home Assistant's main loop, about 1 s per busy day on a desktop (more on a Raspberry
  Pi), and the labelled time in the reply took about as long again. Both now run in the
  background: the main loop is held for at most about 45 ms, and the label histograms
  follow about a second later, exactly as before.
- The first label after a restart or a cleanup applies at once instead of waiting about
  5 s for all of the radar's history to be counted; its histograms follow once counted.

## 1.30.0 - 2026-10-24

- Learn is much faster, with exactly the same results. On five real radars with about
  four weeks of history, a Learn takes about half the processor time it did once a
  radar has learnt before (19% less on the first run after its history or labels
  change). A radar's own Learn now rates each candidate threshold from the few
  stretches of history it changes, instead of re-adding all of them. What Learn works
  out from each finished day is kept beside that day, so the next Learn reads it
  instead of working it out again. It is worked out afresh whenever the day, its
  labels or the integration's code change, and has its own 32 MB disk budget.
- When there is no automatic weight, Learn no longer compares two policies that are
  the same.
- Recorded history takes about half the disk space. Finished days now store each
  reading's end beside the next reading's start (they almost always meet) and their
  time steps in a layout zlib packs better: on a real install, 24.6 MB of history
  became 13.4 MB, with every reading unchanged. Existing days are rewritten once at the
  next restart. Less space also means the storage limit trims history later.
- Day summaries are no longer saved to disk. They took about 0.8 MB per day (more than
  the history itself), loading one was slower than rebuilding it, and they did not fit
  their budget on real installs. Existing summary files are removed at the next
  restart, freeing up to 64 MB.
- The saved state is smaller: each radar keeps a count of its classification segments
  instead of the last 500 of them, which only the count was ever shown from (about a
  quarter of the saved device data). The count is no longer capped at 500.
- Bermuda presence sources: choose the target area from a list of your Home
  Assistant areas instead of typing it (areas already saved by name keep working),
  and optionally set a maximum distance. Within the area, the person then only counts
  as present within that distance of the area's scanner, read from Bermuda's own
  Distance sensor for that device; farther away counts as outside the area, and an
  unknown distance gives no label.
- Graphs, Learn and other history reads no longer fail on very busy days. A day of
  more than about 645,000 recorded changes (possible with 18 gates reporting every
  couple of seconds) was too big for the background worker's temporary-file limit
  on Linux, so its graph or Learn job failed. That limit now matches the size of day
  the worker already accepts.
- Newer evidence is kept when a radar's references are full. Each kind of reference
  evidence keeps its newest 24 periods, but newer direct observations could be
  dropped in favour of older room evidence. The oldest periods now go first.
- References now notice label edits on room-mates. A grouped radar's references
  depend on the other radars' labels in its room. Editing one of them now marks the
  room-mates' references out of date too, and a rebuild that was running during the
  edit is redone instead of installing a stale result.
- A gate that goes unavailable no longer carries its last reading into a new
  recording session; it is recorded as missing until it reports again.
- Just after midnight (UTC), the room estimate no longer briefly disagrees with
  itself before the new day starts recording. This also fixes a test that failed
  20-50 minutes after midnight.
- Exports are taken one at a time: another export request while one is running gets
  "busy, retry shortly" instead of queueing without limit.
- Startup checks the recording state handed back by its worker before using it.
- The learning schedule no longer reads time-zone files on Home Assistant's main
  loop.

## 1.29.1 - 2026-10-23

- Refresh today's graphs much faster. A day graph only recharts its newest few minutes
  when it reloads and keeps the rest; a busy radar-day takes about 2 ms of chart work
  instead of about 170 ms, with exactly the same result. Changing the bucket size,
  thinning, Trim or Clear charts the day afresh.
- Send graphs at less than half the size. Each point now carries only what the panel
  draws, so a busy radar-day reply drops from about 650 KB to about 290 KB, with less
  for Home Assistant to check and the browser to read. Home Assistant still checks
  every reply's exact shape.
- Graphs start faster: each chart's worker no longer loads Home Assistant-side
  machinery it never uses (about 0.07 s less per graph on a desktop, more on slower
  hardware). Home Assistant also checks each worker reply about 2.5 times faster.
- Stop Home Assistant freezing when a label changes before a radar's history is
  counted. After a restart or a cleanup, changing the presence label, pausing
  recording, a label timing out or a room label on a lone radar counted all of that
  radar's history on Home Assistant's main loop, about 3 s per week of history. The
  change now applies at once and the label histograms update in the background a
  few seconds later.
- The panel no longer breaks between a HACS update and the Home Assistant restart.
- The panel uses the full width on wide screens.
- ESPHome package: Presence Found Gates and the new Presence Lost Gates list each gate
  over threshold with its value and threshold. These replace the 36 per-gate "at
  Presence Found/Lost" sensors, which can be deleted in HA after reflashing.

The ESPHome package change needs a rebuild and reflash; the firmware has not been
compiled or tested on hardware yet.

Validation: 2,506 workspace tests passed with 96.97% coverage. The browser suite
passed, and the ESPHome package validated and generated C++ for 1 (with and without
overrides), 2, 3 and 12 radar nodes.

## 1.29.0 - 2026-10-23

- Load room graphs faster. A room's radars are now charted together in one background
  job, up to four radars at a time, instead of one job per radar, so each extra radar
  no longer pays for its own job start. The room graph now appears once its radars are
  ready, rather than one line at a time.
- Remember each gate's energy at the moment the radar finds or loses presence. The
  ESPHome package adds 36 Diagnostic sensors per radar, such as `G3 Move at Presence
  Found` and `G3 Still at Presence Lost`, holding the raw gate energy from the radar
  frame where presence switched on or off. Home Assistant keeps every snapshot in its
  history.
- Name the gates behind each detection. A new Diagnostic text sensor, `Presence Found
  Gates`, lists every gate over its threshold and within Max Move or Max Still
  Distance when presence is found, for example `G3 Move, G4 Move, G4 Still`.
- Show the per-gate threshold numbers as device configuration instead of dashboard
  controls.

The new sensors need the ESPHome package rebuilt and flashed; the firmware change has
not been compiled or tested on hardware yet. Engineering Mode must be on for the
snapshots and the gate list to update.

Validation: 2,470 workspace tests passed with 96.93% coverage (one test that reads the real
clock fails only in the 13 minutes before midnight UTC, before this release too;
it passed when rerun after midnight). The browser suite passed, and the ESPHome package
validated and generated C++ for 1, 2, 3 and 12 radar nodes.

## 1.28.1 - 2026-10-22

- Calculate room and radar graphs about ten times faster. Each graph point is now
  built from whole blocks of stored readings instead of one step per reading per
  gate, so a full-day room graph no longer slows down with every reading recorded.
  Graphs show the same values; a radar that reconnects at the instant it
  disconnected keeps the previous exact calculation.

Validation: 2,478 workspace tests passed with 96.97% coverage. One radar's full
86,400-reading synthetic day charted in 0.23 s instead of 2.33 s on a desktop; new
comparison tests match the previous calculation on random histories with gaps,
unreadable readings, long readings and same-instant reconnects.

## 1.28.0 - 2026-10-22

- A room or zone with exactly one recording radar now trains that radar with its manual
  labels; the radar's own labels still win where both exist, and **Seen by** keeps
  showing what the radar itself saw. That radar is labelled on its room's timeline,
  which lists any labels of its own (saved or live) and can open its editor; View
  radar opens its gate graph.
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

Validation: 2,424 workspace tests passed with 96.96% coverage. Desktop/mobile browser
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

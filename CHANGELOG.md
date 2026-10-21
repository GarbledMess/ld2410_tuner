# Release notes

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

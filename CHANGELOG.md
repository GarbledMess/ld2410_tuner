# Release notes

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

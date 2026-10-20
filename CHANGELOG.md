# Release notes

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

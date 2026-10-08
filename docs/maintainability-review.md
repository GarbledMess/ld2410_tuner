# Maintainability review

This review covers the integration's Python modules, browser panel, repository
tools, tests and package boundaries. It preserves learning and device behaviour.

## Shared ownership

| Rule or mechanism | Owner | Consumers |
| --- | --- | --- |
| Missed-time cost, display score, strict improvement | `calibration/scoring.py` | Individual and joint learning, comparisons, automatic Apply |
| Confidence-weighted time integration | `calibration/intervals.py` | Observation timing and exact room-label boundaries |
| Sample outcomes and episodes | `calibration/metrics.py` | Raw and timing-adjusted diagnostics |
| Retiring completed tasks without removing replacements | `runtime/tasks.py` | Individual and group learning and assessment |
| Numeric field capture | `static/panel/forms.js` | Storage, timing and presence-source settings |
| Package inventory and validation | `tools/validate_package.py` | Local checks and release archive construction |

Shared threshold-mask functions now have public names. The scoring formula,
timing assumptions, persisted report fields and scorer versions are unchanged.

## Deliberately separate

- Human-label boundaries remain exact; sampled energy confidence uses midpoint
  cells. Only their integration arithmetic is shared.
- Individual and group evidence selection remain distinct. Group truth and
  shared recording coverage cannot be replaced with per-device labels.
- A group Apply locks every member and establishes replacement coverage before
  withdrawing existing coverage. It shares the device writer, but retains its
  own validation and reporting.
- Job-specific error messages and cancellation reports stay with their jobs.
  Only task retirement and unobserved-exception handling are common.
- Small request/refresh sequences, domain-specific validation, declarative
  ESPHome gate entries and readable test fixtures remain explicit. A generic
  framework would hide their differences without removing a shared rule.

## Verification

The initial suite passed 479 tests with 94.02% Python coverage. Regression tests
added for this review cover exact versus sampled boundaries, subdivision
invariance, raw/timed sample parity, unrounded improvement and task replacement
during completion or cancellation. Browser regressions cover the affected forms,
including saved drafts and failed submissions.

Final local verification: 487 Python tests passed with 94.00% line coverage
across the integration and tools. Browser regressions, Ruff lint/format,
Prettier, the complexity check (531 functions, maximum 10), diff whitespace
checks and the 1.22.0 release archive build passed. Three new Python helper
modules have full line coverage. The production/tooling Python statement count
fell from 5,348 to 5,334; the small coverage-percentage change reflects removal
of already-covered duplicate code, not a reduction in tested paths.

Exact-function and cross-file block scans found no substantial literal clones;
a normalized Python AST similarity check found no cross-file candidates at its
review threshold. These are review aids, not proof of zero semantic duplication.
The existing CI gates enforce tests, at least 85% Python coverage, lint,
formatting, cognitive complexity at most 10, browser regressions and packaging.
Local verification does not establish live Home Assistant or radar accuracy.

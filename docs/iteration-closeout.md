# Performance and isolation iteration closed - 2026-10-20

The user accepted this iteration as complete. The selected implementation is finished;
there are no unfinished commitments from this iteration.

## Delivered

- One-install HACS integration, with no new dependency or separate app.
- Whole-percentage energy reporting in the ESPHome package and normalization for
  non-package devices; legacy fractional history remains readable.
- Direct byte-column processing, indexed quality collection and bounded caches.
- Mandatory low-priority, resource-limited Linux subprocesses for production numerical
  jobs and heavy storage recovery, conversion, encoding, compression and maintenance.
- Guarded logical acceptance, off-loop filesystem publication/Clear, cancellation
  ownership, bounded admission and storage backpressure without dropping pending work.
- A reusable shared constrained Linux/official HA Container lab outside this repository,
  available through both Codex and Claude skills.

## Accepted evidence and limits

The final constrained HA run passed 2,155 tests with 97.02% coverage, above the retained
96.95% floor. Lint, formatting, maximum complexity 10, package checks and browser
regressions passed. Four learning and four storage children ran while HA answered
257 HTTP requests (p95 14.135 ms, maximum 31.154 ms). All 6,000 synthetic rows survived
recovery and all workers were reaped. Exact diagnostic locations are in [the lab guide](ha-lab.md).

The VM runs Alpine Linux and the official HA Container, not HAOS. It provides real HA,
Linux process controls and constrained CPU/RAM testing. The user accepted keeping this
as the primary environment: test automation/reuse has practical value; HAOS would add
Supervisor/deployment compatibility coverage rather than establish a different engine.
ARM hardware and HAOS-specific behaviour were not tested. Absolute immunity from every
HA, operating-system or disk failure is not claimed.

## Next iteration, deliberately separate

Investigate integer timestamps, further batching and compiled kernels. These are
candidates, not demonstrated wins or unfinished work. Profile current code first,
measure complete-job time and peak memory, and preserve numerical/temporal parity.
Reject changes whose complexity or conversion costs outweigh useful overall gains.
NumPy column conversion was assessed and rejected for lack of meaningful end-to-end
benefit; installing NumPy is not an outstanding task.

## Repository disposition

Keep the work on the existing perf-engine-kernels development branch as a local
checkpoint. No push, merge, publication or production installation is included.
The manifest/released archive remain at 1.24.4; a temporary package build verified
that the new runtime files are included. Release/version selection is separate.

See [implementation and safety boundary](hacs-worker-investigation.md) and the
[historical numerical measurements](engine-optimization-results.md).

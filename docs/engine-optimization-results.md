# Numerical engine progress

**Iteration closed 2026-10-20.** The assessments and checkpoint notes below are
historical. Current delivered scope, accepted validation and the separate next
iteration are recorded in [the closeout](iteration-closeout.md).

Development branch: `perf-engine-kernels`, based on released 1.24.4 (`47d94a9`).
This is development work, not a published release or a selected app architecture.

## Implemented

- An HA-independent numerical column kernel accepts byte/float buffers. Search reads
  selected columns directly in chunks of at most 4096 rows, retaining governor checks.
  It avoids reconstructing every full 18-gate vector before rebuilding search columns.
- Whole-percentage energy normalization at the integration recording boundary covers
  package and non-package devices. Finite readings in [0,100] round upward; missing or
  out-of-range readings remain missing. New sessions normalize the whole live baseline,
  not just the first changed gate. Within-bin changes do not create redundant rows.
- Legacy fractional logs/day files remain readable. The numerical input kernel maps
  those readings to search bytes when needed; no bulk history migration is required.
- Medium histories above 3000 rows use the existing indexed quality collector. The
  50,000-row search evidence budget is unchanged. Separate chronological training and
  evaluation models are preserved.
- An oversized derived day-cache result is returned to its caller without being retained
  or evicting useful smaller results. This limits cache retention estimates, not total RSS.
- Device fitting and room assessment can import without HA. Live registry/recording
  imports occur only in their live adapter functions. A clean-interpreter test verifies it.
- The package inventory includes the new numerical modules; no NumPy dependency or manual
  installation step was introduced. No firmware files were changed.

The package-side opportunity is to publish whole-percentage energy at the device, reducing
noise/transport before HA. The integration does not rely on that change. A future firmware
change should use the same rounding policy; native whole-percentage readings need no change.
Raw live-classifier state parsing is unchanged in this revision; this work normalizes
recording and numerical search inputs. Any separate live-classifier policy change needs
its own inference replay assessment.

## Why upward rounding

For integer threshold T, `ceil(x) > T` is exactly equivalent to `x > T`. The pre-existing
quality histograms and search inputs already used ceil. Applying it earlier avoids float
columns and irrelevant transitions while retaining that behavior. Recorded energy moves
by less than one percentage point; pulse timestamps are not rounded to whole seconds.

One synthetic complete-learning regression produces identical reports for fractional and
whole-percentage histories, excluding cache fingerprints. This is stronger than a one-point
tolerance for that fixture, but is not a universal inference-accuracy guarantee. Codec
tests continue verifying that older fractional files can be decoded without corruption.

## Measurements

All results are Windows synthetic probes, unthrottled, not HA OS/Green measurements.
The probes and their fixtures are in the ignored session directory; no private recordings
were read. Individual timing samples are not statistical performance guarantees.

| Experiment | Result |
| --- | --- |
| Direct selected-column preparation, integer histories | Around 1.5x across 18 gates and 1.8x for one gate in initial runs |
| Indexed quality, 12,000 float rows, cold index | 0.313 s versus 0.531 s ordinary collection |
| Indexed quality, 48,000 float rows, cold index | 1.355 s versus 2.146 s ordinary collection |
| Complete fitting, fresh 12,000-row float history | Ordinary 1.764 s; indexed 1.082 s; indexed plus NumPy conversion 1.085 s |
| Complete fitting, fresh 48,000-row float history | Ordinary 7.059 s; indexed 4.410 s; indexed plus NumPy conversion 4.422 s |
| NumPy float conversion microkernel | Roughly 20–28x; no meaningful complete-fitting improvement in these probes |
| Integer byte-column microkernel | Direct byte access faster than NumPy; full-slice results can reuse the immutable buffer |

Earlier complete-fit timings reused warmed indexes and overstated cold-start gains.
The fresh-input measurements above supersede them. Complete report parity was asserted
apart from documented fingerprints. These timings ran alongside verification and contain
host-load noise; repeated isolated runs on target hardware are required before fine ranking.

**Backend decision:** do not add NumPy for column conversion. Preserve the plain-buffer
kernel boundary for a different compiled implementation if later profiles justify it.
Existing native byte/bitset operations and indexed algorithms are a better demonstrated
fit here. This does not reject NumPy for other kernels.

## Integer time and other types

Whole energy columns already use uint8-compatible bytes, with 255 for missing readings.
Day files already encode timestamps as 32-bit millisecond offsets. Decoded timestamps
and many duration prefixes currently expand into float64 seconds.

A synthetic 200,000-row layout probe compared time/end/prefix buffers: float buffers
used 4,800,008 bytes; 32-bit integer buffers used 2,400,004 bytes. Millisecond offsets
round-tripped exactly. Integer bisect queries were slower in that probe (8.88 ms versus
6.17 ms for the query batch), so compact integers are a memory opportunity, not an
automatic speed guarantee. It measured buffers only, not whole-process memory.

The next representation candidate is per-day uint32 offsets and duration prefixes, using
existing millisecond resolution. A day is at most 86,400,000 ms, so that range fits; use
wider types for cross-day totals. Preserve arbitrary query-boundary clipping and float
weights at explicit boundaries. Never replace compact typed buffers with Python int lists
and assume the result uses less memory. Do not quantize confidence or normalized costs
without a separate correctness/quality assessment.

## Process isolation feasibility

An ignored standalone probe now runs the actual device fitter from a frozen day snapshot
and plain JSON metadata in a fresh interpreter, without importing HA or opening the live
store for writes. The complete report matches the in-process oracle after equivalent JSON
normalization. A 12,000-row run measured 0.800 s directly versus 1.163 s including child
startup; its compressed snapshot was 42,441 bytes. This is a feasibility result, not a
production worker implementation or proof of containment.

Before routing production work through it, implement bounded snapshot creation and
transport, process admission, deadlines, termination, memory/CPU-time limits, cleanup,
schema checks and stale revision rejection. Coordinator-owned atomic writes must remain
separate from computation. Validate room/multi-radar jobs and cancellation as well as
single-device fitting. HA OS/Supervisor resource enforcement still needs a target-runtime
test; Windows probes cannot establish it. HACS versus app remains open.

## Verification

Focused column, ingestion and legacy-codec tests: 100 passed. Complete-report tests include
fractional versus whole-percentage evidence and chronological validation. Browser tests
passed with no browser errors. Lint, formatting, complexity (maximum 10) and package
validation passed. The final integer-ingestion full suite passed **1784 tests with
96.95% coverage**, above the 96.53% floor. The independent read-only review found no
important issues. HA OS/Green resource limits and Supervisor deployment were not tested;
no claim of completed isolation is made.

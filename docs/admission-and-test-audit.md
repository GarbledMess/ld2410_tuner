# Work admission and test-suite audit

Read-only audit of release 1.26.1, 21 October 2026. This document records findings
and a proposed next iteration; it enables no runtime or test changes.

## Valid work still has artificial rejection points

The startup fix was necessary but did not resolve the general policy. The same
history can be accepted at startup and subsequently refused for learning,
reference rebuilding or charts.

Two public synthetic finished-day inputs were encoded with the actual day-file
writer and passed to the actual numerical decoder using its production per-day
budgets. Both failed with `History count budget exceeded`:

| Input | Compressed bytes | Inflated file bytes | Rejection |
|---|---:|---:|---|
| 172,800 rows, no guesses | 988,649 | 4,492,843 | Per-day 131,072-row cap |
| Two rows, 4,097 guess runs | 16,770 | 45,162 | Per-day 4,096-guess cap |

These demonstrate count-based rejection, not excessive measured resource use.
They are decoder admission probes, not complete-job performance measurements.
The row probe used constant-valued columns; changing gate values can create the
same legitimate row count, but no full learning job was measured in this audit.

Other concrete constraints need end-to-end treatment:

| Boundary | Current behavior | Required direction |
|---|---|---|
| `worker/capture.py`, `worker/calculations.py` | Both impose 4,096 guesses per day; capture also limits mutable guesses to 32,768 | Budget resident representations consistently; process larger valid histories in bounded pieces |
| `worker/history.py` | 131,072 rows per day, 12 MiB inflated day ceiling | Separate malformed-file checks from processing capacity; bounded decode/spill for larger valid days |
| `worker/capture.py` | 16 devices, 512 device-days, 512 MiB cumulative source files | Page manifests and visit data incrementally rather than confusing total input size with resident memory |
| `worker/transactions.py` | Clones at most 256 MiB and 8,192 files | Batch storage transactions, retaining atomic ownership and recovery; configuration allows up to 10 GiB/3,650-day retention |
| `worker/flushing.py` | Refuses a pending batch above 131,072 records | Flush bounded prefixes and retain the uncommitted remainder, including late spans and rollover ownership |
| `worker/bridge.py`, `worker/supervisor.py` | Some interactive operations still use fail-fast admission; background and chart/reference/attribution operations defer | Coalesce durable jobs by identity and queue lightweight requests; expose queued progress |
| `worker/limits.py`, `worker/supervisor.py` | CPU and wall exhaustion terminate a child and fail its current job | Retain hard process protection; checkpoint resumable work and give foreground requests turns between pieces |

The resident day cache already evicts entries instead of aborting an aggregate
job when its retention allowance is reached. Optional derived caches also bypass
oversized entries. Preserve these useful patterns.

## Proposed operating policy

Valid work should queue, run incrementally and retain progress. Resource limits
should govern each active piece and resident state, not declare an entire valid
dataset unsupported solely because it is large. Preserve low scheduling priority,
the hard process safety boundary and bounded parent-side allocations. Validate
file identities, ownership, malformed input and corruption strictly.

Queue records must stay small and deduplicated. Chunk completion should release
the worker turn so live requests can run. When pressure prevents safe progress,
retain the request and report its reason. An input that cannot fit one piece needs
splitting or spill-backed processing, not an endless identical retry.

Storage batches must commit only their owned prefix and retain the remainder.
Numerical chunks must carry timing/filter state and combine statistics without
changing global candidate selection, evidence weights or report semantics.
Independent per-chunk fits cannot simply be averaged. This is why raising all
constants is insufficient and why the numerical transport/representation boundary
must be designed before changing individual algorithms.

## Test-suite findings

The committed suite contains 124 Python test files and 1,817 test functions.
Parameterization expands runnable cases further: 120 decorators include 436
literal rows and four dynamically defined input sources. These AST counts are
not collected-case totals. Six untracked performance-test files add 20 functions;
they do not explain the size of the released suite.

Specific consolidation candidates:

- `tests/test_tools.py`: two characterization-updater tests execute all five
  expensive scenario builders three times in total. The same five scenarios run
  individually in `tests/test_characterisation.py`; room assessment is additionally
  rebuilt for compatibility assertions. Generate/reuse each required result once
  and combine assertions on that result. Keep pipeline coverage distinct from
  generator-format checks, using small real synthetic data for the latter.
- `tests/test_automatic_weight.py`: a recursive source-text search for `AUTO_WEIGHT`
  is a migration-era implementation check. Observable weight-policy tests already
  cover the relevant behavior; harmless comments should not break this contract.
- `tests/test_learning_efficiency.py`: several operations each sweep 300-500
  deterministic random cases. Keep named boundaries and representative independent
  oracle comparisons in the routine suite; broader sweeps can have an explicit
  extended/release invocation rather than being repeated for every edit.
- Review frozen pre-refactor parity suites against current behavior owners.
  Retire overlapping transition checks once their distinct boundary contracts are
  represented. Preserve independent numerical oracles and representative memory,
  concurrency, recovery and indexing guards.

Do not cut every large fixture: existing 20,000-row tests prove concrete memory or
narrow-window indexing behavior. Identical test bodies with different malformed
inputs are not automatically redundant. Parameterizing names reduces source
repetition, usually not execution count or cost.

Measure slow-test durations before claiming runtime savings. Prefer one owner for
each behavioral contract, extend an existing case when possible, and require a
distinct regression or resource risk for a new test. Coverage remains a useful
backstop, not a reason to preserve redundant assertions or to test every helper
implementation independently. No numerical target for test count is justified by
this audit alone.

## Next work order

1. Consolidate repeated characterization work and remove obsolete migration
   checks; record before/after duration and retained behavioral coverage.
2. Define queued job/progress and checkpoint ownership once, then apply bounded
   prefix flushing and paged storage transactions without losing durable work.
3. Replace numerical input-size rejections with consistent resident-memory
   accounting, paged manifests and bounded decoding. Address snapshot acquisition
   under the parent recording lock as part of this boundary.
4. Add numerical continuation only where needed to preserve exact global search
   and temporal behavior across worker turns. Use the shared constrained HA lab
   for representative end-to-end capacity and responsiveness checks.

Reuse existing regressions and lab probes. Add only the missing behavioral
counterexamples; do not create a separate test matrix for every new helper.

# Performance, safety and delivery exploration

**Iteration closed 2026-10-20.** The assessments and checkpoint notes below are
historical. Current delivered scope, accepted validation and the separate next
iteration are recorded in [the closeout](iteration-closeout.md).

Assessment date: 2026-10-16. Baseline: safety release 1.24.4 (`47d94a9`).
This is an exploration, not an approved implementation or delivery decision.

The user subsequently authorized autonomous implementation, prioritizing numerical
backend/data representation choices and then whole-percent energies with layered
package/integration/legacy support. See [implementation results](engine-optimization-results.md)
for current changes and fresh benchmarks. The user has since clarified that continuation
is investigative and **HACS is selected**. Current next steps and evidence are in the
[HACS worker investigation](hacs-worker-investigation.md); the app alternatives below
are historical comparison material, not the current roadmap.

## Requirements

One installation must provision everything required. An app may replace HACS entirely;
neither delivery route is preferred in advance. Preserve unattended operation, current
learning semantics and evidence fidelity while maximizing useful capability. Measure
HA interference, worker memory, completion time and recovery together. Low priority
alone cannot guarantee that the shared host will never become unresponsive.

## New profile evidence

Ran the existing synthetic one-hour learning fixture under cProfile, without reading
private test data. Artifacts are in the ignored session directory:
`profile-codex-native-learning.py` and `exploratory-learning.prof`.

| Region | Cumulative seconds | Interpretation |
| --- | ---: | --- |
| Complete fit | 15.476 | Instrumented Windows synthetic run; not a Green benchmark |
| Quality context collection, two calls | 6.157 | About 40% of fit; investigate repeated preparation |
| Human energy extraction | 3.506 | Nested within collection, not additive |
| Day vector reads, 118,800 calls | 1.838 | Nested cost; investigate decoding and repeated lookups |
| Search, three calls | 3.599 | About 23%; workload dependent |
| Recorded evidence reads, two calls | 2.119 | Another preparation target |
| Replay projection | 0.599 | Existing cache already avoids some replay |
| Weighted interval `across` | 0.167 | About 1.1%; target of the earlier NumPy probe |

Cumulative rows overlap and must not be summed. Profiling overhead can change the
relative cost of Python and native functions. Repeated calls may use different scopes;
their presence does not establish that results can safely be reused.

The earlier synthetic interval microbenchmark measured 14.72x kernel speedup, with
additional conversion and memory costs. Even eliminating `across` entirely saves only
about 1.1% of this profiled fit. It is not evidence of a 14.72x learner speedup. Repeat
profiles with longer, sparse, dense and multi-radar histories before generalizing.

## Candidate methods

These options can be combined. Ranking is for investigation, not a promised outcome.

| Method | Where it could help | Principal cost or uncertainty |
| --- | --- | --- |
| Reuse immutable evidence and quality inputs within a job | Avoid repeated day/vector extraction and context construction | Must distinguish scopes and revisions; bound retained memory |
| Batch reads and retain compact column views | Reduce Python object allocation and per-row lookups | Current storage is already columnar; avoid duplicating it in memory |
| Exact incremental scoring | Recompute only affected windows when thresholds change | Existing bitsets and replay caches already help; establish remaining redundancy |
| NumPy | Batched threshold operations, prefix queries and array reductions | End-to-end benefit unproven; conversion and temporary arrays count |
| Cython ahead-of-time kernel | Branchy interval/timing loops that resist vectorization | Native wheel build and architecture/Python compatibility maintenance |
| Small Rust kernel or worker | Native loops and an explicit data boundary | Additional language/toolchain; a full rewrite has no demonstrated justification |
| Numba | Rapid experiment compiling numeric loops | Cold compilation, runtime memory and supported wheel matrix need measurement |
| Arrow / memory mapping | Read-only column interchange and reduced process copies | Protocol and lifetime complexity; benefit depends on snapshot size |
| Polars / DuckDB | Large scans, grouping and aggregation if these dominate | Custom timing replay is not naturally a query-engine workload |
| SQLite transactions / job journal | Durable job state, atomic metadata and recovery | Reliability option, not an established numerical speed improvement |
| Alternative compression | Reduce codec CPU or disk traffic if profiles justify it | Format migration and compatibility; no present evidence this is the bottleneck |
| Browser Web Worker / WASM | Move interactive preview work to the viewing device | Cannot own unattended learning when no browser is open |
| Bounded parallel work | Exploit spare capacity on larger hosts | Competes with HA; one compute worker should be the starting baseline |

Keep exact algorithms as the first line of investigation. Downsampling evidence,
reducing search quality or replacing the optimizer may change capability and requires
separate held-out quality validation. The current adaptive search cannot simply have
all candidates evaluated concurrently without considering its update semantics.

NumPy documents that many numeric operations release the GIL, but shared mutable arrays
can race or crash the interpreter. Use immutable inputs and retain process isolation:
[NumPy thread safety](https://numpy.org/doc/stable/reference/thread_safety.html).
Numba compiles suitable numeric loops, with first-call compilation overhead:
[Numba guide](https://numba.readthedocs.io/en/stable/user/5minguide.html).
Ahead-of-time alternatives should ship prebuilt artifacts, never require a user compiler:
[Cython packaging](https://docs.cython.org/en/latest/src/userguide/source_files_and_compilation.html),
[Rust/Python packaging](https://pyo3.rs/main/building-and-distribution).

## Isolation is a separate dimension

Both HACS and an app can run heavy work in disposable processes. A robust worker boundary
needs bounded admission, input/output sizes, deadlines, cancellation, crash recovery,
revision checks before applying results, and coordinator-owned atomic writes. Avoid
pickling HA objects or sending full histories repeatedly through an unbounded pipe.
Prioritize current interactive requests over obsolete queued computations; coalesce
superseded jobs. Limit native-library thread counts as well as Python worker counts.

On supported Unix systems, `RLIMIT_AS` limits virtual address space and `RLIMIT_CPU`
limits accumulated CPU seconds. Neither supplies a CPU-percentage or RSS quota:
[Python resource limits](https://docs.python.org/3/library/resource.html).
App/container packaging alone does not establish enforceable host resource ceilings.
Actual HA OS permissions and failure behavior must be tested. Threads, subinterpreters
and GIL release do not provide a separate-process failure boundary.

The app's specific advantage is moving recording, coordination, UI serving and numerical
dependencies out of HA Core, with an independent lifecycle. Its costs include an HA API
adapter, event transport/reconnection, migration and image maintenance. HACS can retain
the existing integration surfaces while isolating computation, but leaves a bridge in
Core and shares HA dependency constraints. No measured comparison yet establishes a
winner. See [delivery assessment](app-isolation-and-numpy-assessment.md); its app plan is
conditional. Required remote compute, GPU hardware or a separate manually configured
service would violate the intended ordinary installation experience.

## Evidence to collect next

1. Profile the same engine on several synthetic scales and permitted exported scratch
   histories. Record unprofiled wall/CPU time, peak memory and stage costs too.
2. Test bounded reuse/batched evidence reads first, because this profile identifies a
   substantial target. Prove identical selected settings, quality outputs and gap handling.
3. Compare NumPy with one small compiled-loop candidate on the remaining dominant kernel.
   Include cold startup, conversions and peak memory; do not select from kernel time alone.
4. Exercise the engine across an asynchronous process protocol. Inject hangs, excessive
   allocation, crashes, malformed/oversized output, cancellation and stale results. Verify
   coordinator responsiveness and durable data after termination.
5. Probe supported protected HA OS deployment and architecture combinations for both
   viable delivery routes, using the same worker engine. Measure HA event-loop latency,
   event transport cost, host memory pressure and useful work under a fixed resource budget.
6. Select the simplest one-install route meeting those measured requirements. Do not build
   two complete products just to compare packaging. App-specific API/limits experiments
   and an integration worker experiment can establish feasibility first.

Use a comparison of speed, memory, HA latency, quality, installation compatibility and
maintenance cost. No single weighted score should hide a failed safety or installation
requirement. A faster engine is valuable because it can complete more capable analysis
within a conservative resource budget, not because it should consume all available CPU.

## Follow-up source audit and checks

Two unprofiled executions of the one-hour fixture completed with `status=ok` in 5.730
and 5.806 seconds. The 15.476-second instrumented run has substantial profiler overhead;
its stage percentages are diagnostic leads rather than expected elapsed-time savings.

The two `_quality_context` calls are deliberately different: `_selection` constructs an
`earlier_view` ending at a chronological cutoff. That view clips device and room labels,
resets attribution guesses and prevents reading later physical evidence. Sharing the
full-history quality result with that earlier objective would introduce validation
leakage. Preserve separate models, episode lineage and cutoff-specific attribution.

There is already an exact-summary candidate: `summary_quality.collect` computes physical
histograms from indexes without materializing every vector. It is selected above 50,000
rows. `test_dense_learning` compares the complete learning report between collectors,
apart from their deliberately different fingerprints. `test_summary_quality` covers
gaps, missing values, fractional energies, labels, ownership and source evidence. The
first numeric experiment should compare this existing collector below its current
threshold, including its cold index-building cost. It may avoid a new dependency.
Do not assume the crossover is always 50,000 rows or that every input has equal cost.

Existing physical preparation reconstructs all 18 values in `Day.vector`, then
`rooms.evidence._recorded` selects keys and rebuilds byte columns. Investigate a selected
column reader preserving the exact `ceil` comparison, missing sentinel, explicit ends,
clipping and barriers. Retain raw fractional values where callers need them; the search
byte representation must not silently replace all evidence. Share immutable storage,
not mutable label-derived dictionaries or one large unbounded cache of Python tuples.

The existing day-result cache already uses finished-day identity, arguments and an LRU
policy. Its 64 MiB figure is approximate, and `_keep` stops evicting once one item remains:
one oversized value can exceed it. A proposed tightening is to return oversized computed
values without caching them. That bounds retained cache estimates, not transient job
memory; a disposable worker limit is still required. Live/closing days deliberately
bypass cache reuse and need separate memory measurement. No cache behavior changed here.

Worker contract candidate:

- Coordinator supplies versioned immutable physical columns, explicit coverage, label and
  source metadata, timing, constraints and a revision token. Include the history required
  to derive room attribution; one radar's values alone may be insufficient.
- Worker owns per-job derived objects, chronological validation and bounded caches. Keep
  durable writes and application of settings in the coordinator. Separate raw evidence
  identity from label/model identity.
- Return a bounded result or a reference to a staged result, with job/revision identifiers.
  Coordinator validates the schema and current revisions before atomic acceptance.
- A deadline, crash or cancellation discards the result; no partial settings or evidence
  mutation. Bound protocol buffers and avoid materializing a second full history just
  to serialize it. Shared files/buffers need explicit read-only ownership and lifetimes.

Re-ran the existing summary-quality, dense-learning and day-cache tests: **27 passed**.
The first sandbox run had temporary-directory permission errors; the authorized rerun
passed. These tests establish current fixtures only, not the proposed optimization.

A separate invocation of the existing day benchmark on the same synthetic scratch store
returned `insufficient` for learning after its poll/edit setup. Its 0.009-second result is
excluded from performance comparisons. That harness changes its scratch store, so future
comparative runs must use fresh independent copies and verify successful learning,
equivalent inputs and unchanged evidence before accepting timings.

No production changes or delivery choice were made in this follow-up.

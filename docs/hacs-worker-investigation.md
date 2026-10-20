# HACS production worker isolation - 2026-10-20

**Iteration accepted and closed.** See [the closeout](iteration-closeout.md) for
repository disposition and the separate next iteration.

HACS remains the single installation. Production numerical jobs now run in
bounded disposable Linux subprocesses, with no NumPy or new dependency.
The ESPHome package publishes whole-percentage energies and zero-decimal
metadata while preserving configurable gate rounding.

## Connected production paths

Device learning, comparisons and automatic Apply comparisons use the worker
bridge. Room assessment, joint learning and pre-Apply comparisons, Seen-by,
reference initialization/rebuilding, cold label recounts, asynchronous charts
and export-day encoding use the same boundary. These are enabled routes, not
an unused supervisor prototype.

Storage startup recovery and legacy conversion, journal encoding, finished-day
compression, late-guess splicing, retention planning, histogram rebuilding and
replacement staging now also run in protected children. Private trees preserve
original history when a worker fails or is killed. Ordinary flushes copy only
touched files; idle flushes start no worker.

HA retains device writes, bounded event ingestion, raw-buffer handoff and logical
result acceptance. Existing label, timing, source, history ownership and threshold
revision guards reject stale work. Logical storage acceptance installs pending
in-memory day views before awaiting filesystem publication. Clear immediately
invalidates generations and hides old files; a separate filesystem lock serializes
publication/removal in owned executors without holding recording's memory lock
across disk I/O. Cancellation retains ownership. Failed publication retains staging
and blocks further maintenance rather than silently continuing.

Startup publishes a validated recovered tree with a stable backup, rollback and
interrupted-swap recovery. Shutdown closes numerical admission first, drains Clear
I/O, then uses one dedicated limited storage service for its final save and closes
that service too. Native HA Store persistence remains through HA's storage API.
These controls reduce resource and lifecycle risks; they cannot establish absolute
immunity from every integration, operating-system or storage failure.

## Admission and protection

One numerical operation is active per runtime, with at most one pending request.
Admission precedes view creation. The serialized storage caller waits on existing
jobs when the queue is full, preserving pending durability work without making
early history copies or increasing the worker count. Metadata is checked before copies; aggregate
history capture allows at most 16 devices, 512 days, 1,048,576 mutable rows,
32,768 mutable guesses and 32 MiB of mutable columns. Each mutable day is capped
at 131,072 rows, 4,096 guesses and 12 MiB of raw columns. Finished history uses
a 32-MiB resident LRU and a separate 512-MiB total file-work allowance, so reading
retained days does not accumulate decoded arrays without limit. Summary cache
accounting includes the physical day arrays it retains. Source metadata allows up to 128
devices, but each captured job keeps its separate 16-device limit.
Finished history remains identity-checked file references; mutable data belongs
to a private temporary job directory. The child bounds decoding and allocation.

The same Python interpreter starts with `-I -S -B`, avoiding integration startup
and site dependencies. Before numerical imports or input decoding, the child
requires Linux controls: nice 19, OOM adjustment at least +500, parent-death
SIGKILL, 512 MiB address space, 120 CPU seconds (a diagnostic signal at 119 seconds),
16 MiB file size, 64 open files
and no core dumps. The supervisor enforces a 180-second deadline and capped
output. Requests and results use bounded versioned JSON, without pickle or
arbitrary object reconstruction. Unsupported hosts or missing controls fail
closed; calculations never silently fall back into HA.

Cancellation retains ownership of preparation, process creation, termination,
reaping and temporary-file removal. Cleanup finishes before replacement work is
admitted; cleanup failure closes admission. Shutdown stops and reaps workers.
Already-running bounded executor preparation can extend cancellation cleanup
past the request deadline; ownership is preferred over abandoning work.
Oversized jobs fail explicitly rather than expanding their budget automatically.

Background Seen-by and reference refreshes share the existing caller semaphore
and deduplication. If active and pending worker slots are occupied, they wait
before acquiring views; interactive jobs retain fail-fast admission. Storage
metadata accepts native JSON-compatible tuples and scalar mapping keys through
a bounded normalizer, without weakening the numerical protocol. History date
queries use an in-memory inventory populated during startup and updated after
successful publication, so synchronous snapshots do not scan directories.

## Storage budgets

Storage requests/results are capped at 16 MiB and 1,048,576 metadata nodes.
Private source trees allow at most 8,192 regular files, 16,384 directory entries,
two path components below the history root and 256 MiB. Links and special files
are refused, and copies verify file identity. Raw state handoffs have a 32-MiB
aggregate ceiling and bounded day/row/guess counts. No replay or decompression
is performed when HA installs those raw buffers. Oversized work fails explicitly.

## Verification

The suite covers numerical parity, stale-result rejection, malformed and
oversized inputs, real subprocess limits, cancellation, cleanup and shutdown.
A test-order transport leak was reproduced and fixed with a regression test.
Final constrained Linux HA verification passed **2,155 tests with 97.02%
coverage**, retaining the 96.95% floor. Lint, formatting, complexity and package
checks passed. The complete browser recipe passed with no browser errors.
Four real learning children and four storage children ran while HA answered
257 HTTP requests (p95 14.135 ms, maximum 31.154 ms). Flush/trim/recovery
preserved all 6,000 synthetic rows, and all children were reaped. Results and diagnostic locations are in [the lab guide](ha-lab.md).

The reusable lab implementation, skills, disks and caches live outside this
repository and are shared by projects and both Codex and Claude. The VM uses Alpine Linux and the official HA Container, not HAOS. It is a
resource stress profile, not ARM instruction emulation. Actual HAOS/Supervisor
and aarch64 hardware remain untested. The user accepted this primary test
environment; HAOS compatibility testing is not a blocker to closing this iteration.
No production installation, publication or change to the released archive was made. A temporary
HACS-format archive was built to verify packaging.

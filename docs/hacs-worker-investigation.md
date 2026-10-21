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
early history copies or increasing the worker count. Manual and interactive
requests also wait for a worker turn instead of failing when it is busy. Copied
metadata is bounded (1 MiB, 65,536 nodes); radar, day, row and guess counts are not
admission limits. Each mutable day's columns are capped at 32 MiB, and each decoded
finished day at 128 MiB of resident representation, within the mandatory 512 MiB
address-space limit. Wire bytes are inflated through a temporary file. Per-view day
metadata is spooled to a job-private SQLite index and read lazily in the child.
Finished history uses a 32-MiB resident LRU that evicts, or bypasses an oversized
day, rather than failing the job. Summary cache accounting includes the physical
day arrays it retains.
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

Worker failures include a parent-side diagnostic record even when SIGKILL prevents
the child from writing an error. Bounded off-loop `/proc` sampling every 0.5 seconds
retains the last CPU time, RSS, high-water RSS and virtual size; failure records also
include wall time/deadline, PID, host available memory, and before/after shared-cgroup
memory limits/usage/OOM counters. Missing counters are explicitly unavailable.
Child stderr milestones include installed limits, operation, view/day/file counts,
input bytes and the last reached phase. They contain no device IDs, room names,
history values or recording paths, and success does not add HA log noise.

SIGKILL is named explicitly. An OOM-counter increase is evidence from the shared
cgroup or host, not proof that this particular child was OOM-killed; the last CPU
sample and installed limits also help investigate hard CPU exhaustion. Resource
samples are approximate; the nominal interval is 0.5 seconds, but the last sample
can be older when collection is delayed or unavailable.
Diagnostics are best effort and do not relax protection or replace owned cleanup.

## Storage budgets

Storage requests/results are capped at 16 MiB and 1,048,576 metadata nodes.
Private source trees allow two path components below the history root. Finished
`.day` and `.sum` files, which the store only replaces atomically, are hard-linked
and identity-checked; appendable `.log` files are copied. Filesystems that cannot
link fall back to a streamed copy, and the only size refusal is insufficient free
space for copied bytes. Links and special files are refused. Raw state handoffs are
governed by restored decoded memory (192 MiB), not aggregate row or device counts.
Flushes publish owned bounded prefixes and drain the rest in later turns. No replay
or decompression is performed when HA installs those raw buffers. Oversized work fails explicitly.

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

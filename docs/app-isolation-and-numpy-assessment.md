# Standalone app, isolation and NumPy assessment

**Iteration closed 2026-10-20.** The assessments and checkpoint notes below are
historical. Current delivered scope, accepted validation and the separate next
iteration are recorded in [the closeout](iteration-closeout.md).

Assessed 2026-10-16 against safety release 1.24.4, commit `47d94a9`.

**Superseded delivery choice:** the user has selected HACS. This app comparison is retained
as investigation history, not an implementation recommendation. See the current
[HACS worker investigation](hacs-worker-investigation.md).

Implementation follow-up: [numerical engine results](engine-optimization-results.md).
Whole-percent recording, direct column preparation and indexed quality are implemented
on a development branch. NumPy column conversion did not improve complete cold fitting
in the tested fixtures; it is not a required dependency or a reason to choose an app.

## Recommendation and scope

The broader exploration is now in [performance, safety and delivery options](performance-safety-options.md).
The user is delivery agnostic and welcomes other packages and methodologies. The worker
experiment below is one feasibility probe, not a selected HACS-first roadmap. Fresh
synthetic profiling identifies evidence preparation ahead of the interval microkernel
as an optimization target for that workload.

The delivery decision remains open. The earlier recommendation to prefer an app was
premature: the meaningful comparison is HACS with an isolated worker versus a standalone
app, rather than the current threaded integration versus an app. First validate whether
an isolated worker delivered by HACS meets the safety/latency and installation requirements.
NumPy acceleration and process resource limits do not intrinsically require an app.

If the app path is selected, ship one app image containing the web UI, HA connection,
recorder, learner and any justified numerical dependencies. No HACS integration, companion bridge, manually installed
Python package, MQTT broker or user-created access token is required for that path. The
existing ESPHome devices/integration remain the source of sensor entities and the target
of ordinary HA service calls.

## Balanced comparison: the strongest HACS alternative

| Criterion | HACS plus isolated worker | Standalone app |
| --- | --- | --- |
| One installation | Yes: integration manages its child process and dependencies | Yes: one app image, no companion integration |
| Separate interpreter for heavy jobs | Yes | Yes |
| Kill a hung computation without killing HA | Yes, if parent never waits synchronously and protocol is bounded | Yes, with the same worker lifecycle requirements |
| Worker low priority, CPU-time/address-space limits | Possible on supported OS; needs HA OS validation | Possible; same OS limits, also needs validation |
| Hard CPU percentage or whole-container RSS cap | Not automatically available inside HA Core's container | Not automatically supplied by standard Supervisor app configuration |
| Tuner code remaining inside HA | A small bridge, plus any live logic not moved | None of the custom tuner implementation; HA still serves API events/actions |
| NumPy installation | HA-managed requirements; compatible with HA's Python/package constraints | Included in image; independent from HA Core's Python packages |
| Existing panel and HA registries | Retain direct access and current panel | Replace access with API client, reconnect/cache logic and standalone ingress bootstrap |
| Existing store and migration | Can retain the current owner/format; moving ownership to worker still needs care | Move ownership to app /data; implement and test cutover and rollback |
| Lifecycle and updates | Worker managed by integration; bridge lifecycle remains coupled to HA | Independent app restart/update/logs/backup lifecycle |
| Development/maintenance | Worker protocol, limits and serializable evidence boundary | Those same concerns plus HA API adapter, standalone server, images and migration |
| Deployment coverage | Existing HACS-supported HA installations | Supervisor app on HA OS; standalone Docker would be another deployment experience |

Python provides asynchronous subprocess creation/termination and Unix process limits; neither
feature requires a separate app installation. See [async subprocesses](https://docs.python.org/3/library/asyncio-subprocess.html)
and [resource limits](https://docs.python.org/3/library/resource.html). A worker entry point
must avoid executing the current HA-specific package initializer and must receive plain job
data instead of pickling the runtime. That extraction is common work for both options.

The app's strongest specific argument is moving *all* tuner-owned logic and dependencies
out of HA Core, including recording, live-reference updates, UI serving and coordination.
It is independently restartable and its numerical environment can be pinned independently.
An isolated HACS worker removes heavy computation from HA but leaves a bridge to audit.
That bridge can be small; its existence alone does not demonstrate unacceptable risk.

The case for an app becomes convincing if measurements show material cost/risk remains
in that bridge or the HA dependency environment prevents the required numerical backend,
or if independent lifecycle is an explicit product requirement worth the migration cost.
There is no current measurement establishing those conditions. The observed 75 ms p99
was for threaded 1.24.4 on Windows, not an isolated HACS worker or an app. It cannot choose
between them, and the proposed 50 ms target is an engineering goal, not a demonstrated
threshold at which HA becomes unsafe.

An app can add HA API serialization/reconnect overhead for high-frequency sensor events;
it is not automatically cheaper for HA than forwarding a small relevant event payload to a
child process. Both paths still depend on HA for radar observations in the proposed design:
keeping the app running while HA restarts does not preserve observations that HA never sends.
Measure event transport as well as worker CPU and memory.

## Decision experiment before a product migration

1. Prototype a HACS-managed subprocess using the same immutable evidence and computation
   fixture, low priority, memory/CPU-time limits, deadlines and bounded admission proposed
   for the app. Preserve the existing panel and single installation. Include uncooperative
   CPU, excessive allocation, worker crash and shutdown/reload cases.
2. Measure HA event-loop lateness, bridge CPU, event throughput, lost/unknown coverage,
   peak memory and recovery on Linux/Green, including dense room setup and full learning.
3. Check automatic NumPy installation against the supported HA Python/ABI/package matrix;
   verify that the child can import the installed dependency without user setup.
4. If those gates pass, keeping HACS is the lower-change option for the stated safety and
   speed goals. If they fail because of constraints an app actually removes, run the app
   platform/API spike with the same workload and compare before migrating the full product.

Do not build two full implementations to make this decision. The existing app plan is a
conditional costed work list, not an approved migration or a substitute for this comparison.

This is an assessment and implementation plan, not a claim that the app already exists.
The interim HACS integration is separately committed as 1.24.4 and its ZIP is built.
The user subsequently authorized merging the safety release to the default branch; `main`
was fast-forwarded and pushed to `47d94a9`. This is not HACS catalogue publication or app deployment.
Docker is not available in the current workspace environment, and no protected HA OS/Green
instance was exercised. App conclusions combine code/API inspection with the isolated
numeric experiment below; container and permission claims still need the planned smoke test.

The user's final installation decision supersedes earlier dual-install proposals: an app
must stand alone. Keeping an HACS bridge is not an acceptable app acceptance criterion.
The 1.24.4 release can remain a usable checkpoint without committing to maintaining two
complete product implementations indefinitely.

## Why the app can stand alone

Supervisor provides `ws://supervisor/core/websocket` and a REST proxy, authenticated with
the app's supplied `SUPERVISOR_TOKEN`; no manually copied HA token is necessary. Enable
`homeassistant_api`, and keep that token exclusively on the server. Read events and states,
fetch registry metadata, and call the existing number/switch/button services through this
connection. The endpoint and token are documented in [app communication](https://developers.home-assistant.io/docs/apps/communication/).

Serve the UI through ingress. The app owns `/data`, which is persistent and included in
its backup policy. Build and publish tested aarch64 (Green) and amd64 images in CI; do not
compile NumPy on the user's Green. App images and ingress are supported deployment
mechanisms, described in [app configuration](https://developers.home-assistant.io/docs/apps/configuration/)
and [app development](https://developers.home-assistant.io/docs/apps).

An app is installed from the app store/repository, not HACS. Adding its repository and
installing/starting that one app is the complete software installation. Normal device/room
configuration inside its UI is still necessary; no second tuner package should be needed.

## What changes in this repository

| Current code | App replacement or reuse |
| --- | --- |
| `__init__.py`, `config_flow.py` | App entry point, readiness state, health endpoint and shutdown lifecycle |
| `runtime/coordinator.py` | App-owned runtime/state; no HA objects passed into jobs |
| `runtime/discovery.py`, `entity_index.py`, `rooms/groups.py` | HA WebSocket client plus bounded entity/device/area cache |
| `runtime/websocket.py` | App HTTP/WebSocket API retaining operation names and result schemas where practical |
| `calibration/device_io.py`, `recovery.py`, `timing_config.py` | API-backed state reads and existing HA service calls, preserving pacing and reported-state guards |
| `runtime/engineering.py`, `schedule.py` | App tasks with explicit lifecycle, deadlines and reconnect behavior |
| `history/` codecs, policy, indexes, budget | Reuse semantics and day format; app owns its `/data` files |
| Calibration, timing replay, objective and presence algorithms | Reuse, then accelerate selected numeric kernels behind parity tests |
| `static/ld2410-tuner-panel.js`, `panel/activity.js`, `panel/sources.js` | Standalone HTML bootstrap, app transport and app-provided entity-picker inventory |

Fifteen Python modules directly import Home Assistant. Most domain computation does not,
but importing any submodule currently executes the HA-specific package `__init__.py`.
The first extraction must establish an HA-independent package boundary; moving only a
worker entry script is insufficient. Avoid redesigning the scoring or recording formats.

The UI is largely ordinary HTML/JavaScript, with its main server calls centralized in
`panel/activity.js`. Replace `_hass.callWS` with an app transport, and `_hass.states` with
the cached inventory needed by the source picker. Add an ordinary HTML entry point and
relative asset/WebSocket paths compatible with the ingress prefix. Do not depend on a
parent window's HA object or expose the Supervisor token to the browser.

Registry listing/subscription commands and their permissions are a compatibility spike:
verify them on the supported HA versions using the Supervisor identity. Generic state
and service APIs exist, but that alone does not prove every current registry operation.

## Process and resource boundaries

Use a lightweight app coordinator for the HA connection, recording, UI and scheduling,
with one disposable computation process for an expensive job. These processes are inside
one installed app, not separate installations. The worker receives bounded job data and
immutable day snapshots, not an HA runtime or executable pickled requests. It returns
proposals/results; the coordinator validates revisions and exclusively authorizes device
writes and atomic publication of changed history. Worker death must not interrupt recording.

The app removes computation from HA's Python interpreter and restart lifecycle. However,
container isolation alone is not a hard CPU or memory budget. Current Supervisor app
configuration exposes `ulimits`, but its schema does not expose general `cpu_quota` or
`mem_limit` fields. Unknown configuration fields are removed. This was checked against
the [Supervisor app schema](https://github.com/home-assistant/supervisor/blob/main/supervisor/apps/validate.py)
and [Docker app implementation](https://github.com/home-assistant/supervisor/blob/main/supervisor/docker/app.py).

Implement these boundaries without requiring host privileges:

- Set worker OS priority low before importing numerical libraries or loading evidence.
- Apply a hard worker address-space limit and per-job CPU-time limit; recycle the worker
  per job so CPU-time accounting does not accumulate across unrelated jobs. `RLIMIT_AS`
  limits virtual address space, not RSS; size it using measured Python/NumPy mappings as
  well as working buffers. `RLIMIT_CPU` caps cumulative CPU seconds, not CPU percentage.
  These distinctions are specified by [Python resource limits](https://docs.python.org/3/library/resource.html).
- Enforce wall deadlines, termination then forced kill, bounded request/result sizes,
  bounded caches/queues and streamed history/export I/O. Never retry a failing job forever.
- Keep recording independent from the computation queue. On overflow/disconnection record
  unknown coverage and expose degradation; do not silently invent continuity or lose peaks
  through indiscriminate event coalescing.
- Keep numerical library threads at one through the worker's own environment; do not
  change HA Core's environment. Use bounded candidate batches, never an unbounded
  candidate-by-observation-by-gate tensor.

Hard CPU bandwidth/RSS container limits need appropriate cgroup delegation or container
configuration. A standard protected app cannot be assumed to have that access. The
[kernel cgroup documentation](https://docs.kernel.org/admin-guide/cgroup-v2.html) distinguishes
CPU weights from `cpu.max` bandwidth and memory ceilings. Prototype this on HA OS/Green
before promising a hard percentage cap. Do not request Docker socket access or privileged
host access merely to make an isolation claim. If exact bandwidth limits are unavailable,
state the actual protections: separate interpreter, low priority, hard worker address-space
and CPU-time ceilings, bounded jobs and deadlines. This is stronger than 1.24.4 pacing,
but not an absolute guarantee against every form of host contention.

## NumPy: worthwhile, but needs algorithm changes

The prior work log identifies approximately 2,900 timing windows replayed across 3,200
candidates as a remaining bottleneck. `rooms/replay.py:Projector.project/_window` repeatedly
walks window objects, `DurationGroup._spans` applies event timing, and `Objective._rates`
unions projected intervals and integrates weighted time. Installing NumPy alone changes
none of those loops. Existing Python integer masks and `bytes.translate` already perform
substantial work in native code, so do not replace them indiscriminately.

Recommended acceleration order:

1. Add an array-oriented evidence view once per immutable job: offsets, starts/ends,
   observed coverage, label/provenance boundaries and weights. Preserve 255/missing values,
   float-valued gate readings, gaps and the existing duration weighting.
2. Implement a small vectorized weighted-integral kernel using `searchsorted`, indexed
   prefix/weight arrays and reductions. Keep the current implementation as its oracle.
3. Profile the full learner again, then batch independent timing windows/candidates. Preserve
   hold/on/off semantics, lower/upper bounds, separate windows, selected evidence weights,
   episode lineage and deterministic tie-breaking. The search decision must remain sequential
   where later candidates depend on earlier results.
4. Retain bounded numeric buffers and reuse converted arrays across candidates. Benchmark
   cold conversion/import, warm scoring, cache eviction and peak memory, not just arithmetic.
5. Only enable a numeric backend after full-result parity and end-to-end speed/memory gates.
   Do not combine this with fewer windows, weaker evidence or changed search quality.

An isolated synthetic probe of the existing weighted-time `across` operation used 100,001
boundaries, 2,900 query spans and four shared-time masses. Median of three samples, each
100 calls: Python 1.035 s; NumPy 0.0703 s, approximately **14.7x for this kernel**. Array
conversion took 0.039 s and the persistent NumPy arrays used 6.91 MiB, in addition to the
Python inputs and temporary arrays. Results agreed within the 1 ms duration tolerance.
This is **not** an end-to-end learning speedup or an ARM benchmark. The probe and its
dependencies are isolated in ignored scratch; production dependencies were not changed.

## Installation implications

For a standalone app, install a pinned NumPy wheel while building the image and test the
result on both architectures. Users pull the complete image; no `pip`, shell, compiler or
extra app is required. Keep dependency changes separate from migration/scoring changes.

If the HACS route were chosen instead, HA can install manifest requirements automatically.
Its current dependency constraints pin NumPy to `2.3.2` for the musllinux build environment;
blindly requiring the newest PyPI NumPy can conflict with HA. Verify every supported HA
release, Python ABI and ARM wheel. The relevant mechanisms are [integration requirements](https://developers.home-assistant.io/docs/creating_integration_manifest/),
[HA dependency constraints](https://github.com/home-assistant/core/blob/dev/homeassistant/package_constraints.txt)
and [HA's NumPy wheel index](https://wheels.home-assistant.io/musllinux-index/numpy/).
A mandatory manifest dependency is installed before setup, so an in-code Python fallback
cannot rescue a failed dependency installation. None of that complexity is required by the
app-only path, whose NumPy version is independent of HA Core's environment.

## Existing-data migration

Provide one migration operation inside the app, not a second package installation. Discover
an old tuner config entry through HA, prevent concurrent ownership, then import the old JSON
and day files from a read-only HA configuration mount into staging under `/data`. The mount
permission is broader than the individual files; restrict what the migration code opens.
Never write the source store or silently remove HACS files.

HA implements an admin-protected `config_entries/disable` command, including a response
that signals when a restart is required. Verify the app identity can use it and confirm
the entry is unloaded before copying. If it cannot be unloaded, stop migration and explain
the required restart rather than importing from a live writer. See [config entry API code](https://github.com/home-assistant/core/blob/dev/homeassistant/components/config/config_entries.py).

Reuse existing conversion readers for pre-day-file stores, preserve labels/references/saved
results, verify staged data and available disk space, then atomically activate it. Make
interruption/retry idempotent. Preserve the original source for rollback and identify that
new recordings after cutover belong to the app. Test backup/restore and restart during
copy/conversion. No real recordings or identifiers may enter tracked fixtures or images.

## Decision and implementation gates

Start with the smaller isolated HACS-worker experiment above. The app remains a valid
alternative, with a protected aarch64 API/limits proof required before a full port or any
promise of hard host-wide CPU limits. Whichever delivery model wins, accelerate numerical
kernels separately from lifecycle/storage changes. Keep 1.24.4 as the interim checkpoint.
The conditional app task sequence is in `docs/app-isolation-and-numpy-plan.md`.

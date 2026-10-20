# Historical Standalone App and NumPy Plan — Superseded

**Status: do not execute. HACS is selected.** The current continuation is investigation,
not implementation. See [HACS worker investigation](hacs-worker-investigation.md).
The material below is retained as historical comparison only.

> For agentic workers: use the executing-plans skill to implement this plan task by task.
> This document is an assessment handoff, not authorization to deploy, migrate user data,
> write devices or publish a new app. Follow the current session's explicit authorization.

**Goal:** Replace the tuner with one independently installed Home Assistant app, preserving
its behavior and recordings while separating expensive computation from HA and accelerating
proven numeric bottlenecks.

**Architecture:** One app container contains an asynchronous HA client, persistent recorder,
web UI and job supervisor. A disposable low-priority worker process handles expensive jobs
with enforced address-space/CPU-time limits and deadlines. NumPy ships in the image; no
HACS companion or user-installed dependencies are required.

**Tech stack:** Python 3.13, asyncio/aiohttp, NumPy initially evaluated at 2.3.2, the existing
JavaScript panel and day-file codecs, Supervisor Core API proxy, ingress, aarch64/amd64
container images, pytest/coverage and Playwright. Resolve and lock tested image digests and
Python dependency hashes during the packaging spike rather than publishing floating tags.

The assessment and primary sources are in `docs/app-isolation-and-numpy-assessment.md`.
Keep the committed 1.24.4 checkpoint independently usable while this work is incomplete.

## Task 1: Prove the protected app platform and permissions

Files: create `repository.yaml`, `apps/ld2410_tuner/config.yaml`,
`apps/ld2410_tuner/Dockerfile`, `apps/ld2410_tuner/requirements.lock`,
`tests/app/test_package.py`; add a local-only smoke entry point in the app package.

- [ ] Write failing schema/package tests requiring one app, aarch64+amd64, ingress, persistent
      `/data`, `homeassistant_api: true`, no HACS dependency, no host network, Docker socket
      or additional host privileges. Confirm the tests fail before scaffolding.
- [ ] Build both images with the pinned NumPy wheel already installed. Start from a normal
      Python image if that avoids Alpine ABI/build constraints; an HA base image is optional.
- [ ] Run a protected HA OS smoke test through the Supervisor proxy: authenticate, list
      relevant states and entity/device/area registries, subscribe to updates and validate
      service-call permissions without performing device writes.
- [ ] Record actual Python ABI, architecture, library imports, baseline address space/RSS,
      supported rlimits, cgroup delegation and startup latency. Do not assert CPU quota
      availability merely because the app is containerized.
- [ ] Gate: stop and resolve permissions/runtime capability gaps before porting the product.

## Task 2: Establish an HA-independent domain package

Files: create `src/ld2410_tuner/engine/` and its `__init__.py`/version metadata; port the
existing domain modules from `history/`, `calibration/`, `presence/`, `rooms/` incrementally;
modify package configuration and `tests/conftest.py`/domain test imports as needed.

- [ ] Add a failing subprocess import test: engine imports work when importing `homeassistant`
      is deliberately rejected. Also verify codecs load without an HA manifest path.
- [ ] Extract pure computation and the day-store format without changing algorithms or
      storage versions. Separate the fifteen direct HA import sites from the engine.
- [ ] Replace runtime-object assumptions only at narrow boundaries: entity inventory/state,
      history snapshots, scheduling, device commands and persistence. Avoid generic services
      or a wholesale learner rewrite.
- [ ] Run existing synthetic/golden domain tests after each extraction. Preserve current
      label/truth precedence, gaps, missing readings, timing, evidence budgets and scoring.
- [ ] Gate: all existing regression cases pass and coverage stays at least 96.53%.

## Task 3: Implement the bounded HA client and device-command adapter

Files: create `src/ld2410_tuner/app/ha_client.py`, `inventory.py`, `device_io.py`,
`runtime.py`; tests in `tests/app/test_ha_client.py` and `test_device_io.py`.

- [ ] Test first with a local fake HA WebSocket server: authentication, result correlation,
      timeouts, entity filters, inventory refresh, HA restart, reconnect and token-safe logs.
- [ ] Test recovery after connection loss as unknown coverage rather than a held reading
      across the missing interval. Preserve original event timestamps and unavailable states.
- [ ] Implement one persistent connection, bounded entity caches and subscriptions limited
      to relevant entities. Refresh registry metadata on changes rather than per radar/tick.
- [ ] Port reported-state verification, command pacing, device locks, cancellation and the
      existing engineering/recovery rules. Use existing HA actions; no new integration service
      registration, broker or direct ESP protocol is required.
- [ ] Test duplicate/reordered events, removed entities and malformed messages. Ambiguous
      service-call completion must not automatically repeat a potentially completed write.
- [ ] Gate: replay existing device-I/O scenarios against the fake API with no real writes.

## Task 4: Implement resource-limited computation jobs

Files: create `src/ld2410_tuner/app/jobs.py`, `worker.py`, `limits.py`, `protocol.py`;
tests in `tests/app/test_workers.py` and `test_limits.py`.

- [ ] Write failing tests for a CPU runaway, excessive allocation, hung worker, oversized
      result, stale generation and worker crash. Keep each fixture bounded by a test watchdog.
- [ ] Spawn a fresh worker per expensive job. Apply low OS priority and hard per-worker
      address-space/CPU-time limits before numerical imports/evidence loading. Set numerical
      thread environment variables in that child only.
- [ ] Define versioned job DTOs with bounded sizes and immutable history snapshot references;
      do not pickle HA runtime objects or arbitrary executable callables across the boundary.
- [ ] Implement one active worker, bounded admission, progress, wall deadlines, graceful
      termination followed by forced kill and bounded retry/backoff. Keep UI/recording live.
- [ ] Only the coordinator publishes results, commits prepared history, or writes devices;
      verify snapshot revisions and generations immediately before acceptance.
- [ ] Validate address-space ceilings against real NumPy mappings. Distinguish CPU seconds
      from CPU percentage in configuration and status. Probe cgroup quotas separately; lack
      of delegated control must not silently produce a false hard-cap claim.
- [ ] Gate: resource-limit tests protect the coordinator and HA heartbeat even when worker
      code ignores cooperative checkpoints. Measure this on protected HA OS/aarch64.

## Task 5: Port the recorder, scheduling and app API

Files: create `src/ld2410_tuner/app/recording.py`, `storage.py`, `schedule.py`,
`server.py`, `routes.py`; tests in `tests/app/test_recording.py`, `test_routes.py`.

- [ ] Test that every existing domain operation retains its request/result behavior; use
      deterministic clocks and synthetic stores, including DST and midnight rollover.
- [ ] Move persistence into `/data`; reuse day codecs, optional indexes and guarded commits.
      Keep one writer, bounded buffers and short periodic flushes independent of computation.
- [ ] Port learning, comparison, reference, cleanup, source and room jobs through the worker
      protocol. Keep device operations in the coordinator with the existing validation guards.
- [ ] Stream exports/imports rather than constructing unlimited JSON/base64 in coordinator
      memory. Test partial requests, cancellation, disk-full and interrupted publication.
- [ ] Gate: recording continues while a worker is busy/killed and no stale worker can overwrite
      labels, current history, references or newer job results.

## Task 6: Run the existing panel as a standalone ingress UI

Files: create `apps/ld2410_tuner/rootfs/index.html` and standalone UI bootstrap/transport;
adapt `static/ld2410-tuner-panel.js`, `panel/activity.js`, `panel/sources.js` in the app
asset tree. Port `tests/panel.cjs` and `tests/browser/` through the app server fixture.

- [ ] Add failing browser tests with no HA custom-panel host, no injected `hass` object and
      a non-root ingress prefix. Verify relative assets, WebSocket URLs and authentication.
- [ ] Supply a transport for the existing `_call` operation vocabulary and an entity inventory
      for source pickers. Reuse rendering/history/job controls rather than replacing the UI.
- [ ] Keep the Supervisor token server-side. Enforce ingress access and admin requirements
      before device-changing requests; do not trust arbitrary spoofed user headers or expose
      a host port that bypasses ingress authentication.
- [ ] Re-run mobile, reconnect, job persistence, timeline, DST, error and duplicate-action tests.
- [ ] Gate: browser suite passes through real app ingress on HA OS, not only a direct port.

## Task 7: Migrate existing integration data without a companion install

Files: create `src/ld2410_tuner/app/migration.py`; app configuration gets a read-only
HA-config mount; tests in `tests/app/test_migration.py` and UI migration cases.

- [ ] Test fresh install, current day files and older JSON layouts using synthetic fixtures.
      Test permission failure, active old writer, insufficient disk, restart and rollback.
- [ ] Provide one in-app migration operation. Discover the old config entry; use the supported
      API to disable/unload it only as part of the user-selected cutover, then verify it stopped.
      If HA requires a restart, report that state and do not copy from an active writer.
- [ ] Copy the source to staging under `/data`, convert/verify there and atomically activate.
      Preserve source data and record completion so retry is idempotent. Never delete HACS
      files or overwrite the original store.
- [ ] Define backup/restore and a clear rollback path; restoring the old integration does not
      automatically include recordings made only by the app after cutover.
- [ ] Gate: app can migrate with no running tuner integration or second installed package.

## Task 8: Accelerate numeric work behind existing oracles

Files: create `src/ld2410_tuner/engine/numeric/intervals.py` and later `timing.py`;
adapt the ported `calibration/intervals.py`, `duration.py`, `event_timing.py` and
`rooms/replay.py` only after profiling; add `tests/numeric/` and a synthetic benchmark tool.

- [ ] Start with failing equivalence tests against the current weighted-time implementation:
      empty inputs, exact boundaries, gaps, zero weights, missing data and shared-time masses.
- [ ] Build immutable arrays once per job and vectorize weighted integrals. The scratch probe
      measured 14.7x for this isolated kernel; do not use that as an overall speed claim.
- [ ] Profile the complete learner to quantify the remaining replay/window cost. Add batching
      only where dependencies permit, with a hard working-buffer budget and useful cancellation
      boundaries. Retain efficient integer bit masks where they already win.
- [ ] Verify selected thresholds, episode lineage, held-out metrics and deterministic ties on
      synthetic and private external-copy evaluations. Use the existing millisecond-scale
      duration tolerance, and retain exact final guards where strict improvement authorizes
      writes. Do not weaken comparisons to accommodate changed decisions.
- [ ] Benchmark cold startup/import/conversion, warm full Learn, cache eviction and peak memory
      on amd64 and aarch64. Separate compute CPU time from intentionally throttled wall time.
- [ ] Gate: demonstrable end-to-end improvement with no quality regression, acceptable memory
      and bounded HA heartbeat delay. Keep the Python oracle until this is established.

## Task 9: App release and cutover

Files: add `.github/workflows/app-validate.yml`, `app-release.yml`, app README/DOCS,
CHANGELOG and architecture documentation; adapt package verification tools for app artifacts.

- [ ] Build versioned aarch64/amd64 images containing all dependencies; install an image on
      clean HA OS without pip, compiler, token copying, HACS or a companion integration.
- [ ] Run full tests/coverage/lint/format/complexity/browser checks and container smoke tests.
- [ ] On Green, measure initial migration/cleanup plus room setup, reconnect, daily learning,
      overnight jobs, concurrent exports, backup/restore, worker runaway and memory failure.
      Use p99 HA lateness <50 ms and worst <250 ms as explicit starting targets, reporting
      measured limits and failures rather than reclassifying misses as success.
- [ ] Verify that the app image's resource claims match protected HA OS capabilities. A hard
      CPU percentage guarantee remains conditional on verified quota enforcement.
- [ ] Review the final combined change and privacy boundaries. Publish only when authorized;
      keep release artifacts and recovery instructions independently usable.

## Recommended first deliverable

If the app option is selected, complete Tasks 1 and the resource-limit prototype from Task 4 before the full port. This
answers the hard questions cheaply: one protected app, no companion install, usable API
permissions, correct ingress, NumPy availability, meaningful limits and HA responsiveness.
Then port behavior before enabling numerical changes, so any regression has a clear cause.
# Historical alternative — not for execution

The user has selected HACS. This conditional app plan is superseded by the
[HACS worker investigation](hacs-worker-investigation.md).

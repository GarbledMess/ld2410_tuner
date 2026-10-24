"""The one owner of reference rebuild policy: events mark stale, rebuilds run on schedule or
on demand, off the Home Assistant event loop.

A full rebuild reads a radar's whole retained history and, for a grouped radar, room
attribution and every member's recorded guesses: seconds per radar on a small machine.
Events (device and room label edits, Seen by taps, membership, recording and registry
changes, room-source finalises and restart recovery) therefore only ``mark_stale``: they
record when the radar's references went out of date (``references.STALE``, persisted with
the device) and never compute. Live ``reference_training.observe`` stays incremental.

``queue`` is the only rebuild entry point (``refresh`` queues and waits). It is called by
the daily schedule tick (the overnight pass before learning, or a references-only pass at
the same time when learning is off) and by the panel's "Refresh references" action
(``refresh_references``). It queues radars; one worker at a time
(sharing Seen by's semaphore, as executor work holds the GIL) copies each queued radar's
history view on the event loop, computes in the executor and installs the result back on
the loop. Radars queued while a pass is waiting or running join the next pass. A result is
discarded, and the radar queued again, when its label revision, a related manual room
label revision or a room peer's labels changed while it computed.

A pass takes each radar's stale mark when it copies the history; an event during the pass
marks it again. A failed (while copying or computing), discarded or cancelled pass puts the
earliest mark back, so a radar is never shown as current without a rebuild that saw its
latest inputs. ``busy`` tells the snapshot which radars are queued or computing.

The worker is a background task, so Home Assistant's stop never waits for it. Unload
(``stop``) drops the queue and cancels the pass in flight; stale marks stay and are saved.
Afterwards nothing is queued or installed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from copy import deepcopy

from ..history import pace, recording
from ..rooms import attribution, truth
from ..worker import bridge, priority
from . import reference_training
from .references import STALE

_LOGGER = logging.getLogger(__name__)


def mark_stale(runtime, device_ids, now=None) -> None:
    """Record that these radars' references are out of date. Cheap: no rebuild runs.
    The earliest time is kept until a rebuild that saw the change installs."""
    now = time.time() if now is None else now
    devices = runtime.data["devices"]
    for key in device_ids:
        if key in devices:
            _mark(devices[key], now)


def _mark(device, since):
    if since is not None:
        device[STALE] = min(device.get(STALE, since), since)


def queue(runtime, device_ids=None, *, reason) -> list[str]:
    """The one rebuild entry point: queue these radars (``None``: every stale radar) for a
    rebuild in the executor and return the radars queued, without waiting."""
    devices = runtime.data["devices"]
    if device_ids is None:
        wanted = sorted(key for key, device in devices.items() if STALE in device)
    else:
        wanted = sorted(key for key in set(device_ids) if key in devices)
    if not wanted or runtime._reference_closed:
        return []
    _LOGGER.debug("Refreshing references of %s (%s)", wanted, reason)
    runtime._reference_pending.update(wanted)
    task = runtime._reference_task
    if task is None or task.done():
        task = runtime._reference_task = runtime.hass.async_create_background_task(
            _worker(runtime), "ld2410_tuner reference rebuilds"
        )
    return wanted


async def refresh(runtime, device_ids=None, *, reason) -> list[str]:
    """``queue`` these radars and return once their rebuild is installed (or failed)."""
    wanted = queue(runtime, device_ids, reason=reason)
    if wanted:
        await asyncio.wait({runtime._reference_task})
    return wanted


def busy(runtime, device_id) -> bool:
    """Whether a rebuild of this radar is queued or computing (cheap; for the snapshot)."""
    return device_id in runtime._reference_pending or device_id in runtime._reference_running


async def refresh_references(runtime, device_id=None) -> dict:
    """The panel action: queue one radar, or every stale radar, and return at once."""
    if device_id is not None and device_id not in runtime.data["devices"]:
        raise ValueError("Unknown device")
    ids = None if device_id is None else [device_id]
    with priority.interactive():
        return {"queued": queue(runtime, ids, reason="requested")}


async def stop(runtime) -> None:
    """On unload: queue and install nothing more, drop the backlog and cancel the pass in
    flight. An executor job cannot be interrupted; its result is never installed."""
    runtime._reference_closed = True
    runtime._reference_pending.clear()
    task = runtime._reference_task
    if task is not None and not task.done():
        task.cancel()
        await asyncio.wait({task})


async def _worker(runtime):
    try:
        while runtime._reference_pending:
            async with runtime._room_attribution_semaphore:
                # Taken only now, so requests that arrived while waiting share this pass.
                runtime._reference_running = set(runtime._reference_pending)
                runtime._reference_pending.clear()
                done = await _pass(runtime, sorted(runtime._reference_running))
            for job, result in done:
                _install(runtime, job, result)
    finally:
        runtime._reference_running = set()


async def _pass(runtime, device_ids):
    """Copy each radar's inputs on the loop and compute in the executor. On any failure the
    marks already taken are put back and nothing installs: a failing rebuild must not be
    retried in a loop."""
    jobs, shared = [], {}  # ``shared``: the members' guesses, built once for the job.

    def prepare():
        for key in device_ids:
            if (job := _prepare(runtime, key, shared)) is not None:
                jobs.append(job)
        return {
            "views": {job["device_id"]: job["view"] for job in jobs},
            "parameters": {
                "jobs": [
                    {"device_id": job["device_id"], "view_key": job["device_id"]} for job in jobs
                ]
            },
        }

    try:
        results = await bridge.calculate(runtime, "references", prepare)
    except (asyncio.CancelledError, pace.Stopped):
        _restore_all(runtime, jobs)
        raise
    except Exception:
        _LOGGER.exception("Could not rebuild room references")
        _restore_all(runtime, jobs)
        return []
    return list(zip(jobs, results, strict=True))


def _prepare(runtime, device_id, shared=None):
    """On the event loop: copy what the rebuild reads and the revisions that must hold."""
    device = runtime.data["devices"].get(device_id)
    if device is None:
        return None
    view = recording._history_view(runtime, device_id)
    view.data["devices"][device_id]["reference_profile"] = deepcopy(
        reference_training.profile(device)
    )
    attribution.prepare(runtime, view, device_id, shared)
    saved = view.data["room_snapshot"] or {"own": [], "groups": {}}
    related = truth.related_to(saved["groups"], saved["own"])
    members = sorted(set(saved.get("members", ())) - {device_id})
    return {
        "device_id": device_id,
        "view": view,
        "related": related,
        "members": members,
        "stamp": _stamp(runtime, device_id, related, members),
        "since": time.time(),
        "stale": device.pop(STALE, None),  # Events from now on mark the radar again.
    }


def _stamp(runtime, device_id, related, members):
    """What a result must still match: the radar's label revision, its related manual room
    label revisions and the labels of the room peers its room truth was read from."""
    manual = runtime.data.get("room_label_revision", {})
    devices = runtime.data["devices"]
    return (
        devices.get(device_id, {}).get("label_revision", 0),
        tuple(manual.get(key, 0) for key in related),
        tuple(attribution.member_signature(devices.get(key, {})) for key in members),
    )


def _restore_all(runtime, jobs):
    for job in jobs:
        device = runtime.data["devices"].get(job["device_id"])
        if device is not None:
            _mark(device, job["stale"])


@pace.background
def compute(jobs):
    """In the executor: each radar's rebuilt periods."""
    results = []
    for job in jobs:
        view, device_id = job["view"], job["device_id"]
        device = view.data["devices"][device_id]
        cutoff = reference_training.rebuild_human(view, device)
        periods = device["reference_profile"]["periods"]
        results.append(
            {
                "cutoff": cutoff,
                "periods": [p for p in periods if reference_training.owned(p, cutoff)],
            }
        )
    attribution.release(job["view"] for job in jobs)
    return results


def _install(runtime, job, result):
    """On the event loop: install unless the inputs it was computed from changed."""
    if runtime._reference_closed:
        _restore_all(runtime, [job])
        return
    device_id = job["device_id"]
    device = runtime.data["devices"].get(device_id)
    if device is None:
        return
    if _stamp(runtime, device_id, job["related"], job["members"]) != job["stamp"]:
        _restore_all(runtime, [job])
        runtime._reference_pending.add(device_id)  # Recomputed by the worker's next pass.
        return
    reference_training.splice(device, result["periods"], result["cutoff"], job["since"])
    runtime._schedule_save()

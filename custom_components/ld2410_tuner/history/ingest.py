"""Ingestion into the day store: delivered gate changes at their own time, automatic guess
run changes, and explicit ends of coverage. Nothing is stored that did not change.

A radar's coverage starts with its full current vector (``runtime._live``) and lasts until an
explicit end: recording paused or the radar removed, storage blocked, a configuration recovery,
or the integration stopping. A reading that is unavailable or not a number makes only that gate
missing. A clock that steps back records at the newest recorded time, never before it.
"""

import time

from ..const import HISTORY_KEYS
from ..engine.columns import percentage
from . import dayfile, guesses

GATE = {key: index for index, key in enumerate(HISTORY_KEYS)}


def change(runtime, device_id, updates, timestamp):
    """Record ``updates`` (gate key -> reading, None when missing) delivered at ``timestamp``.
    False when the radar is not being recorded (its coverage is ended) or nothing is known."""
    days = runtime._days
    if days is None:
        return False
    if not _accepting(runtime, device_id):
        end(runtime, device_id, timestamp)
        return False
    values = _changed(days.current(device_id), runtime._live.get(device_id, {}), updates)
    if values is None:
        return False
    if values:
        days.append(device_id, dayfile.Change(_time(days, device_id, timestamp), values))
    else:
        days.observe(device_id, timestamp)
    return True


def _changed(current, live, updates):
    gates = {GATE[key]: percentage(value) for key, value in updates.items() if key in GATE}
    if current is None:  # A new session: everything currently known.
        vector = {gate: percentage(live.get(key)) for key, gate in GATE.items()}
        vector.update(gates)
        return vector if any(value is not None for value in vector.values()) else None
    return {gate: value for gate, value in gates.items() if current[gate] != value}


def _accepting(runtime, device_id):
    device = runtime.data["devices"].get(device_id)
    return (
        device is not None
        and device.get("recording_enabled", True)
        and not runtime._storage_status.get("blocked")
        and device.get("configuration_recovery", {}).get("status") != "running"
    )


def _time(days, device_id, timestamp):
    last = days.last(device_id)
    return timestamp if last is None else max(timestamp, last)


def guess(runtime, device_id, timestamp, sample_guess):
    """Record the radar's guess at ``timestamp`` (``guesses.guess`` or None) while its coverage
    is open: a new run only where ``guesses.continues`` says the open one does not continue."""
    days = runtime._days
    if days is None or days.current(device_id) is None:
        return
    open_run = days.run(device_id)
    if open_run is not None and guesses.continues((open_run[0], None, *open_run[1:]), sample_guess):
        return
    if sample_guess:
        state, percent, basis = sample_guess
        fields = (state, guesses.level(percent), basis)
    elif open_run is not None:
        fields = (None, 0, 0)  # The open run ends; no guess follows it.
    else:
        return
    days.append(device_id, dayfile.Guess(_time(days, device_id, timestamp), *fields))


def confirm(runtime, device_id, start, end, percent):
    """External presence confirmed for the earlier ``[start, end)`` (a source's buffered
    period): recorded now, it fills only time the radar left without a guess."""
    days = runtime._days
    if days is None or not _accepting(runtime, device_id):
        return False
    now = _time(days, device_id, time.time())
    if min(end, now) <= start:
        return False
    level = guesses.level(percent)
    days.append(
        device_id,
        dayfile.GuessSpan(now, start, min(end, now), "present", level, guesses.EXTERNAL),
    )
    return True


def end(runtime, device_id, timestamp):
    """End the radar's coverage (and guess run) at ``timestamp``, if open."""
    days = runtime._days
    if days is None or (days.current(device_id) is None and days.run(device_id) is None):
        return
    days.append(device_id, dayfile.End(_time(days, device_id, timestamp)))


def stop(runtime, timestamp):
    """The integration stops: every open coverage ends now."""
    if runtime._days is None:
        return
    for device_id in runtime._days.recording():
        end(runtime, device_id, timestamp)

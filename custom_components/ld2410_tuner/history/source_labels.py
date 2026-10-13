"""Confirm buffered external presence without changing energies or human labels."""

from ..presence import reference_training
from . import guesses


def confirm_presence(runtime, device_id, start, end, confidence):
    """Present, from an external source, over the samples in ``[start, end)`` that the radar
    left without a guess while the source's buffer ran. ``confidence`` is in percent."""
    if end <= start:
        return
    device = runtime.data["devices"][device_id]
    reach = guesses.GAP  # The samples around the stretch decide where its cells end.
    window = sorted(
        (ts, row)
        for ts, row in runtime._iter_history_samples(device, since=start - reach, until=end + reach)
        if ts < end + reach
    )
    known = guesses.of(runtime, device_id, start - 2 * reach)
    confirmed = {ts: row for ts, row in window if start <= ts < end and not known.of_sample(ts)}
    if not confirmed:
        return
    present = guesses.guess("present", confidence, guesses.EXTERNAL)
    runs = guesses.from_samples((ts, present if ts in confirmed else None) for ts, _ in window)
    runs = guesses.join(known, guesses.pending(runtime, device_id), runs)
    runtime._flush_history_block(device_id)  # Closed runs are the stored ones, so the edited ones.
    device["guess_runs"] = guesses.upsert(device.get("guess_runs", []), runs)
    for timestamp, row in confirmed.items():
        reference_training.observe(
            runtime, device, timestamp, row, "present", confidence / 100, external=True
        )
    observations = device.setdefault("auto", {}).setdefault("observations", {})
    observations["present"] = observations.get("present", 0) + len(confirmed)
    runtime._history_cache.clear()
    runtime._schedule_save()

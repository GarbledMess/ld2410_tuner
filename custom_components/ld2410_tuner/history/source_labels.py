"""Confirm buffered external presence without changing energies or human labels."""

from ..calibration.constants import SAMPLE_SECONDS
from ..presence import reference_training
from . import dayfile, guesses, ingest, spans


def confirm_presence(runtime, device_id, start, end, confidence):
    """Present, from an external source, over the time in ``[start, end)`` that the radar left
    without a guess while the source's buffer ran. ``confidence`` is in percent."""
    if end <= start:
        return
    known = guesses.of(runtime, device_id, start).runs
    unguessed = list(dayfile.gaps(start, end, [(run[0], run[1]) for run in known]))
    if not unguessed or not ingest.confirm(runtime, device_id, start, end, confidence):
        return
    device = runtime.data["devices"][device_id]
    seconds = 0.0
    for low, high in unguessed:
        for span in spans.events(runtime, device_id, start=low, end=high):
            reference_training.observe_span(
                runtime,
                device,
                span.start,
                span.end,
                span.values,
                "present",
                confidence / 100,
                external=True,
            )
            seconds += span.end - span.start
    observations = device.setdefault("auto", {}).setdefault("observations", {})
    observations["present"] = observations.get("present", 0) + seconds / SAMPLE_SECONDS
    runtime._history_cache.clear()
    runtime._schedule_save()

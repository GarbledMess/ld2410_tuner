"""The poll's recording work for one radar.

The current readings go through ``ingest.change``: only what differs from the recorded vector
is stored, so the poll records nothing that events already delivered, and catches what they
missed (the startup baseline). Every ``HISTORY_SAMPLE_INTERVAL`` the radar's guess is recorded
(``ingest.guess``), and live references and labelled histograms take the physical spans newly
observed since the last time, weighted by their duration, not by how often the poll ran.
"""

import math

from ..calibration.constants import SAMPLE_SECONDS
from ..const import HISTOGRAM_BINS, HISTORY_KEYS, HISTORY_SAMPLE_INTERVAL
from ..presence import reference_training
from . import guesses, ingest, spans

# Live reference evidence is folded in batches: references are long-term distributions, and
# folding every poll re-encoded the open period and rebuilt the classifier's reference pool.
REFERENCE_BATCH_SECONDS = 60


def poll(runtime, device_id, device, values, now, guess):
    if not ingest.change(runtime, device_id, {key: values.get(key) for key in HISTORY_KEYS}, now):
        return
    state = runtime._history_runtime.setdefault(device_id, {})
    if now - state.get("last_sample", 0.0) < HISTORY_SAMPLE_INTERVAL:
        return
    previous, state["last_sample"] = state.get("last_sample"), now
    sample_guess = guesses.guess(*guess)
    ingest.guess(runtime, device_id, now, sample_guess)
    if sample_guess:
        observations = device.setdefault("auto", {}).setdefault("observations", {})
        observations[guess[0]] = observations.get(guess[0], 0) + 1
    if previous is not None:
        pending = state.setdefault("reference_spans", [])
        for span in spans.events(runtime, device_id, start=previous, end=now):
            _observe(runtime, device, span, guess)
            pending.append((span, guess))
    _apply_batch(runtime, device_id, device, now)


def _apply_batch(runtime, device_id, device, now):
    """Fold the radar's waiting reference evidence once a minute, at most one radar per
    tick: radars started together would otherwise all fold (and rebuild their classifier
    references) on the same tick of the event loop."""
    pending = runtime._history_runtime.get(device_id, {}).get("reference_spans")
    if not pending or now - pending[0][0].start < REFERENCE_BATCH_SECONDS:
        return
    if runtime.__dict__.get("_reference_batch_at") == now:
        return  # Another radar folded on this tick; this one waits for the next.
    runtime._reference_batch_at = now
    _references(runtime, device, pending)
    pending.clear()


def settle(runtime):
    """Apply live reference evidence still waiting for its batch (before every save and at
    stop), so saved references are complete."""
    for device_id, state in getattr(runtime, "_history_runtime", {}).items():
        pending = state.get("reference_spans")
        device = runtime.data["devices"].get(device_id)
        if pending and device is not None:
            _references(runtime, device, pending)
        if pending:
            pending.clear()


def _observe(runtime, device, span, guess):
    label = runtime._history_label_at(device, span.start)
    if label in ("present", "not_present"):
        _add_duration(runtime, device, label, span)


def _references(runtime, device, pending):
    """Fold a batch of ``(span, guess)`` into the radar's references in one pass."""
    reference_training.observe_spans(
        runtime,
        device,
        [
            (
                span.start,
                span.end,
                span.values,
                inferred if percent else None,
                percent / 100,
                basis == guesses.EXTERNAL,
            )
            for span, (inferred, percent, basis) in pending
        ],
    )


def _add_duration(runtime, device, label, span):
    weight = (span.end - span.start) / SAMPLE_SECONDS
    for key, value in zip(HISTORY_KEYS, span.values, strict=True):
        if value is None:
            continue
        series = device.setdefault("histograms", {}).setdefault(
            key, {"present": [0] * HISTOGRAM_BINS, "not_present": [0] * HISTOGRAM_BINS}
        )
        series[label][math.ceil(value)] += weight
        runtime._compress_histogram(series[label])

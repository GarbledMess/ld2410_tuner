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
        for span in spans.events(runtime, device_id, start=previous, end=now):
            _observe(runtime, device, span, guess)


def _observe(runtime, device, span, guess):
    inferred, percent, basis = guess
    reference_training.observe_span(
        runtime,
        device,
        span.start,
        span.end,
        span.values,
        inferred if percent else None,
        percent / 100,
        external=basis == guesses.EXTERNAL,
    )
    label = runtime._history_label_at(device, span.start)
    if label in ("present", "not_present"):
        _add_duration(runtime, device, label, span)


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

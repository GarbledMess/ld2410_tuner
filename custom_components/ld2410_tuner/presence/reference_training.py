"""Connect independent corrections and confirmed observations to room references."""

import time
from copy import deepcopy

from ..calibration.constants import MIN_AUTO_CONFIDENCE
from ..const import HISTORY_KEYS
from . import references


def profile(device):
    current = device.get("reference_profile")
    if not current or current.get("version") != references.VERSION:
        current = references.empty()
        device["reference_profile"] = current
    return current


def observe(runtime, device, timestamp, row, *, external=False):
    manual = runtime._manual_history_state(device, timestamp)
    if manual == "unknown":
        return  # An explicit exclusion is not permission to use an inferred label.
    values = {key: value for key, value in zip(HISTORY_KEYS, row, strict=False) if value <= 100}
    if manual in references.STATES:
        references.observe(profile(device), values, manual, "human", 1.0, timestamp)
        return
    label = {1: "present", 2: "not_present"}.get(row[18]) if len(row) >= 20 else None
    confidence = row[19] / 100 if label else 0
    source = "entity" if external else "radar"
    if confidence < (MIN_AUTO_CONFIDENCE if external else 0.80):
        return
    if not external and not profile(device)["periods"]:
        return  # Bootstrap guesses cannot establish an independent reference.
    references.observe(profile(device), values, label, source, confidence, timestamp)


def rebuild_human(runtime, device):
    """Rebuild retained human evidence after an edit; keep older learned summaries."""
    from ..history.labels import _history_label_reader
    from ..history.policy import retention_seconds

    current = profile(device)
    cutoff = time.time() - retention_seconds(runtime.data)
    current["periods"] = [
        p for p in current["periods"] if p["source"] != "human" or p["end"] < cutoff
    ]
    label_at = _history_label_reader(device)
    references.observe_many(current, _human_observations(runtime, device, label_at))
    device.get("auto", {}).pop("filter", None)


def _human_observations(runtime, device, label_at):
    for timestamp, row in runtime._iter_history_samples(device):
        label = label_at(timestamp)
        if label in references.STATES:
            values = {
                key: value for key, value in zip(HISTORY_KEYS, row, strict=False) if value <= 100
            }
            yield timestamp, values, label, "human", 1.0


def correct(runtime, device, ranges):
    current = profile(device)
    # Entity/inferred periods are summaries, so remove the whole overlapping
    # contribution rather than pretending we can precisely subtract old samples.
    current["periods"] = [
        p
        for p in current["periods"]
        if p["source"] == "human"
        or not any(p["start"] < end and p["end"] >= start for start, end in ranges)
    ]
    rebuild_human(runtime, device)
    device_id = next(key for key, value in runtime.data["devices"].items() if value is device)
    runtime._auto_runtime.pop(device_id, None)


def reset_entities(device):
    if "reference_profile" in device:
        profile(device)["periods"] = [
            p for p in profile(device)["periods"] if p["source"] == "human"
        ]
    device.get("auto", {}).pop("filter", None)


async def initialize(runtime):
    """Import timestamped human history once, off the Home Assistant event loop."""
    for device_id, device in runtime.data["devices"].items():
        if device.get("reference_profile", {}).get("version") == references.VERSION:
            continue
        snapshot = deepcopy(device)
        view = runtime._history_view(device_id)
        view.data["devices"][device_id] = snapshot
        await runtime.hass.async_add_executor_job(_import_history, view, snapshot)
        device["reference_profile"] = snapshot["reference_profile"]


def _import_history(runtime, device):
    rebuild_human(runtime, device)
    current = profile(device)
    if current["periods"]:
        return
    # Old histogram-only stores have no observation times. Preserve their useful
    # distributions, explicitly recording import time and reduced certainty.
    legacy = device.get("history_legacy_histograms", {})
    timestamp = device.get("history_legacy_since") or time.time()
    for state in references.STATES:
        histograms = _legacy_bins(legacy, state)
        if histograms:
            current["periods"].append(
                {
                    "source": "human",
                    "state": state,
                    "start": timestamp,
                    "end": timestamp,
                    "confidence": 0.8,
                    "count": max(sum(b.values()) for b in histograms.values()),
                    "data": references.encode(histograms),
                    "imported_legacy": True,
                }
            )


def _legacy_bins(legacy, state):
    result = {}
    for key, series in legacy.items():
        if key not in HISTORY_KEYS:
            continue
        bins = {str(i): count for i, count in enumerate(series.get(state, [])[:101]) if count > 0}
        if bins:
            result[key] = bins
    return result

"""Autolabelling operations on the shared runtime state."""

from __future__ import annotations

import asyncio
import math
import time
from collections import defaultdict
from typing import Any

from homeassistant.core import callback

from ..calibration.settings import DEFAULT_AUTOMATIC_WEIGHT
from ..const import (
    ABSENT_BIAS_MAX,
    ABSENT_BIAS_MIN,
    FEEDBACK_BIAS_DECAY,
    FEEDBACK_BIAS_STEP_UP,
    FEEDBACK_LOG_MAX,
    HISTOGRAM_BINS,
    HISTORY_KEYS,
    MIN_SAMPLES,
    PRESENT_BIAS_MAX,
    PRESENT_BIAS_MIN,
)
from ..history import ingest
from . import references, sources
from .inference import (
    HUMAN_REFERENCE,
    MODEL,
    at_rest,
    classified,
    confirm_estimate,
    estimate_presence,
    hold,
    reclassify,
)


@callback
def sample_devices(runtime, _now=None) -> None:
    from ..rooms import room_sources

    _sample_devices(runtime)
    room_sources.sample_rooms(runtime, time.time())


def _classify_auto(runtime, device: dict[str, Any], values: dict[str, float]) -> dict[str, Any]:
    """Classify the readings ``values``."""
    auto = device.setdefault("auto", {})
    if auto.get("model") != MODEL:
        auto.update(model=MODEL, all_histograms={}, filter={}, calibration={})
    hist = auto.setdefault("all_histograms", {})
    expected = [f"g{info['gate']}_{info['kind']}" for info in device.get("entities", {}).values()]
    profile = device.get("reference_profile", {})
    reference, cap = references.distributions(profile, time.time())
    result = estimate_presence(
        values,
        reference if profile else device.get("histograms", {}),
        hist,
        auto.setdefault("filter", {}),
        time.time(),
        expected,
        auto.get("calibration", {}),
    )
    _reference_confidence(result, profile, reference, values, cap)
    _observe_baseline(runtime, hist, values, 1, result["presence_probability"])
    return result


def _observe_baseline(runtime, hist, values, count, probability):
    """Warm up from observations, then freeze the unlabelled baseline during likely occupancy
    so a stationary person does not become background."""
    if not count:
        return
    for key, value in values.items():
        h = hist.setdefault(key, [0] * HISTOGRAM_BINS)
        if sum(h) < MIN_SAMPLES or probability < 0.7:
            h[int(round(value))] += count
            runtime._compress_histogram(h)


def _reference_confidence(result, profile, reference, values, cap):
    if not profile.get("periods"):
        return
    novel = references.unfamiliar(profile, values)
    result["unfamiliar_gates"] = novel
    result["confidence"] = min(result["confidence"], 0.65 if novel else cap)
    if not reference:
        return
    result["basis"] = "adaptive-guided"
    result["manual_guidance"] = any(p["source"] == "human" for p in profile["periods"])
    for detail in result["top_gates"]:
        if detail["source"] == HUMAN_REFERENCE:
            detail["source"] = "age-weighted room references"


def _update_auto_state(
    runtime,
    device_id: str,
    device: dict[str, Any],
    result: dict[str, Any],
    now: float,
) -> None:
    old, label = _confirmed_label(runtime, device_id, result, now)
    auto = device.setdefault("auto", {})
    auto["last_classification"] = {
        "state": "unknown",
        "confidence": result["confidence"],
        "score": result["score"],
        "active_gates": result["active_gates"],
        "top_gates": result["top_gates"],
        "timestamp": now,
        "basis": result.get("basis"),
        "buffered": result.get("buffered", False),
        "sources": result.get("sources", []),
        "model": result.get("model"),
        "unfamiliar_gates": result.get("unfamiliar_gates", []),
        "presence_probability": result.get("presence_probability"),
    }
    if label == "unknown":
        auto["last_classification"].update(state="unknown", confidence=0.0)
        segments = auto.get("segments", [])
        if segments and segments[-1].get("end") is None:
            segments[-1]["end"] = now
        return
    auto["last_classification"]["state"] = label
    segments = auto.setdefault("segments", [])
    if old != label or not segments or segments[-1].get("end") is not None:
        if segments and segments[-1].get("end") is None:
            segments[-1]["end"] = now
        segments.append(
            {
                "start": now,
                "end": None,
                "state": label,
                "confidence": result["confidence"],
                "score": result["score"],
                "basis": result.get("basis"),
                "sources": result.get("sources", []),
            }
        )
    if len(segments) > 500:
        del segments[:-500]


def _confirmed_label(runtime, device_id, result, now):
    state = runtime._auto_runtime.setdefault(device_id, {})
    old = state.get("state", "unknown")
    if result.get("basis") == "external":
        if state.get("model") != result["model"]:
            state.clear()
            old = "unknown"
        state.update(state=result["state"], model=result["model"], timestamp=now)
        return old, result["state"]
    return old, confirm_estimate(result, state, now)


def auto_learning_summary(
    device: dict[str, Any], weight: float = DEFAULT_AUTOMATIC_WEIGHT
) -> dict[str, Any]:
    auto = device.get("auto", {})
    observation_counts = sum(auto.get("observations", {}).values())
    segments = auto.get("segments", [])
    last = auto.get("last_classification")
    if last and time.time() - last.get("timestamp", 0) > 15:
        last = None
    counts = defaultdict(int)
    for seg in segments:
        counts[seg.get("state", "unknown")] += 1
    feedback_log = auto.get("feedback", [])
    feedback_summary = {}
    for label in ("present", "not_present"):
        entries = [f for f in feedback_log if f.get("label") == label]
        if entries:
            feedback_summary[label] = {
                "correct": sum(1 for f in entries if f.get("correct")),
                "total": len(entries),
            }
    return {
        "enabled": device.get("recording_enabled", True),
        "profile": references.summary(device.get("reference_profile", {}), time.time()),
        "stale_since": device.get(references.STALE),
        "observations": observation_counts,
        "observations_by_state": dict(auto.get("observations", {})),
        "segments": len(segments),
        "segments_by_state": dict(counts),
        "last": last,
        "role": "confidence_weighted_training",
        "weight": weight,
        "feedback": feedback_summary,
        "calibration": dict(auto.get("calibration", {"present_bias": 0.0, "absent_bias": 0.0})),
        "note": _weight_note(weight),
    }


def _weight_note(weight):
    return (
        f"Automatic evidence weight {weight:g}, scaled by confidence (0 = automatic evidence "
        "only breaks ties; 1 = automatic evidence counts up to a quarter of the human evidence). "
        "Automatic Apply still requires an improvement on human labels."
    )


def record_auto_feedback(runtime, device_id: str, correct: bool) -> dict[str, Any]:
    """Record a Correct/Incorrect tap on the last auto classification.

    Nudges a small per-device calibration bias: an "Incorrect" tap raises
    the bar for whichever label was actually shown (present or not
    present) so that specific device needs stronger evidence next time;
    a "Correct" tap slowly relaxes that bias back down. This is
    intentionally asymmetric - fast to react to a mistake, slow to
    relax - so a couple of wrong reads can't be undone by one lucky
    confirmation, but sustained correct feedback still recovers
    sensitivity over time.
    """
    device = runtime.data["devices"].get(device_id)
    if not device:
        raise ValueError("Unknown device")
    auto = device.setdefault("auto", {})
    last = _feedback_reading(auto)
    label = last["state"]
    calibration = auto.setdefault("calibration", {"present_bias": 0.0, "absent_bias": 0.0})
    if label == "present":
        bias = float(calibration.get("present_bias", 0.0))
        bias = bias + FEEDBACK_BIAS_STEP_UP if not correct else bias - FEEDBACK_BIAS_DECAY
        calibration["present_bias"] = max(PRESENT_BIAS_MIN, min(PRESENT_BIAS_MAX, bias))
    else:
        bias = float(calibration.get("absent_bias", 0.0))
        bias = bias + FEEDBACK_BIAS_STEP_UP if not correct else bias - FEEDBACK_BIAS_DECAY
        calibration["absent_bias"] = max(ABSENT_BIAS_MIN, min(ABSENT_BIAS_MAX, bias))

    feedback_log = auto.setdefault("feedback", [])
    feedback_log.append(
        {
            "timestamp": time.time(),
            "label": label,
            "correct": bool(correct),
            "score": last.get("score"),
            "confidence": last.get("confidence"),
        }
    )
    if len(feedback_log) > FEEDBACK_LOG_MAX:
        del feedback_log[:-FEEDBACK_LOG_MAX]
    runtime._schedule_save()
    return {"ok": True, "label": label, "calibration": dict(calibration)}


def _feedback_reading(auto):
    last = auto.get("last_classification")
    if (
        not last
        or time.time() - last.get("timestamp", 0) > 15
        or last.get("state") not in ("present", "not_present")
    ):
        raise ValueError("No confident automatic classification to give feedback on yet")
    if last.get("basis") == "external":
        raise ValueError("Correct external labels in Past presence labels or adjust the source")
    return last


async def async_sample_devices(runtime, _now=None):
    """The periodic poll yields between radars and never overlaps another periodic poll."""
    from ..rooms import room_sources

    if getattr(runtime, "_sampling_active", False) or getattr(runtime, "_sampling_stopped", False):
        return
    runtime._sampling_active = True
    try:
        for device_id in list(runtime.data["devices"]):
            if getattr(runtime, "_sampling_stopped", False):
                return
            _sample_devices(runtime, (device_id,))
            await asyncio.sleep(0)
        room_sources.sample_rooms(runtime, time.time())
    finally:
        runtime._sampling_active = False


def _sample_devices(runtime, device_ids=None):
    for device_id in runtime.data["devices"] if device_ids is None else device_ids:
        device = runtime.data["devices"].get(device_id)
        if device is not None:
            _sample_device(runtime, device_id, device)


def _sample_device(runtime, device_id, device):
    if device.get("configuration_recovery", {}).get("status") == "running":
        runtime._source_runtime.pop(device_id, None)
        ingest.end(runtime, device_id, time.time())
        return
    values = _read_energies(runtime, device)
    runtime._live[device_id] = values  # Drop unavailable readings, never carry them forward.
    if not values:
        runtime._auto_runtime.pop(device_id, None)
        runtime._source_runtime.pop(device_id, None)
        device.get("auto", {}).pop("last_classification", None)
        ingest.change(runtime, device_id, dict.fromkeys(HISTORY_KEYS), time.time())
        runtime._schedule_flush()
        return
    if not device.get("recording_enabled", True) or runtime._storage_status.get("blocked"):
        runtime._source_runtime.pop(device_id, None)
        ingest.end(runtime, device_id, time.time())
        return
    now = time.time()
    _classify_tick(runtime, device_id, device, values, now)
    runtime._record_history_sample(device_id, values, now)
    runtime._schedule_flush()
    runtime._schedule_state_save(now)


def _classify_tick(runtime, device_id, device, values, now) -> None:
    """Bring the device's last classification up to ``now``: an external source decides when
    it reports (its model replaces the confirmation state, and with it the cadence); otherwise
    the radar's readings are classified, or the last result reused."""
    result = sources.estimate(runtime, device, device_id, now)
    if result is not None:
        runtime._update_auto_state(device_id, device, result, now)
    elif not _reuse(runtime, device_id, device, values, now):
        _classify_radar(runtime, device_id, device, values, now)


def _inputs(device):
    """What a classification of the radar's readings also depends on, besides the readings:
    the filter and the reference periods (replaced by rebuilds, resets and corrections),
    how many periods there are (a new one starts) and the feedback calibration."""
    auto = device.get("auto", {})
    periods = device.get("reference_profile", {}).get("periods")
    return (
        auto.get("filter"),
        periods,
        len(periods or ()),
        tuple(sorted(auto.get("calibration", {}).items())),
    )


def _cadence(runtime, device_id, device):
    """The state of the radar's last classification; empty when its inputs have changed."""
    cadence = runtime._auto_runtime.get(device_id, {}).get("cadence", {})
    known, now = cadence.get("inputs", ()), _inputs(device)
    same = known and all(a is b for a, b in zip(known[:2], now[:2], strict=True))
    return cadence if same and known[2:] == now[2:] else {}


def _reuse(runtime, device_id, device, values, now) -> bool:
    """Reuse the last classification for readings that changed nothing notable (see
    ``inference.reclassify``): it stays current, and the filter and confirmation, which
    integrate over elapsed time, see no gap."""
    cadence = _cadence(runtime, device_id, device)
    auto = device.get("auto", {})
    last = auto.get("last_classification")
    if not cadence or not last or reclassify(cadence, values, now):
        return False
    hold(auto["filter"], runtime._auto_runtime[device_id], now)
    # The reading joins the baseline as read, as when classified, so it stays proportional to time.
    _observe_baseline(runtime, auto["all_histograms"], values, 1, auto["filter"]["probability"])
    last["timestamp"] = now
    return True


def _classify_radar(runtime, device_id, device, values, now) -> None:
    auto = device.setdefault("auto", {})
    before = auto.get("filter", {}).get("probability", 0.5)
    result = runtime._classify_auto(device, values)
    runtime._update_auto_state(device_id, device, result, now)
    confirmed = auto["last_classification"]["state"]
    rest = at_rest(before, result, auto["filter"], confirmed, now)
    cadence = runtime._auto_runtime[device_id].setdefault("cadence", {})
    classified(cadence, values, now, rest)
    cadence["inputs"] = _inputs(device)


def _read_energies(runtime, device):
    values = {}
    for entity_id, info in device.get("entities", {}).items():
        state = runtime.hass.states.get(entity_id)
        try:
            value = float(state.state) if state else float("nan")
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and 0 <= value <= 100:
            values[f"g{info['gate']}_{info['kind']}"] = value
    return values

"""Autolabelling operations on the shared runtime state."""

from __future__ import annotations

import math
import time
from collections import defaultdict
from typing import Any

from homeassistant.core import callback

from ..calibration.fitting import AUTO_WEIGHT
from ..const import (
    ABSENT_BIAS_MAX,
    ABSENT_BIAS_MIN,
    FEEDBACK_BIAS_DECAY,
    FEEDBACK_BIAS_STEP_UP,
    FEEDBACK_LOG_MAX,
    HISTOGRAM_BINS,
    MIN_SAMPLES,
    PRESENT_BIAS_MAX,
    PRESENT_BIAS_MIN,
)
from .inference import MODEL, confirm_estimate, estimate_presence


@callback
def sample_devices(runtime, _now=None) -> None:
    _sample_devices(runtime)


def _classify_auto(runtime, device: dict[str, Any], values: dict[str, float]) -> dict[str, Any]:
    auto = device.setdefault("auto", {})
    if auto.get("model") != MODEL:
        auto.update(model=MODEL, all_histograms={}, filter={}, calibration={})
    hist = auto.setdefault("all_histograms", {})
    expected = [f"g{info['gate']}_{info['kind']}" for info in device.get("entities", {}).values()]
    result = estimate_presence(
        values,
        device.get("histograms", {}),
        hist,
        auto.setdefault("filter", {}),
        time.time(),
        expected,
        auto.get("calibration", {}),
    )
    # Warm up from observations, then freeze the unlabelled baseline during
    # likely occupancy so a stationary person does not become background.
    for key, value in values.items():
        h = hist.setdefault(key, [0] * HISTOGRAM_BINS)
        if sum(h) < MIN_SAMPLES or result["presence_probability"] < 0.7:
            h[int(round(value))] += 1
            runtime._compress_histogram(h)
    return result


def _update_auto_state(
    runtime,
    device_id: str,
    device: dict[str, Any],
    result: dict[str, Any],
    now: float,
) -> None:
    state = runtime._auto_runtime.setdefault(device_id, {})
    auto = device.setdefault("auto", {})
    auto["last_classification"] = {
        "state": "unknown",
        "confidence": result["confidence"],
        "score": result["score"],
        "active_gates": result["active_gates"],
        "top_gates": result["top_gates"],
        "timestamp": now,
        "basis": result.get("basis"),
        "model": result.get("model"),
        "presence_probability": result.get("presence_probability"),
    }
    old = state.get("state", "unknown")
    label = confirm_estimate(result, state, now)
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
            }
        )
    if len(segments) > 500:
        del segments[:-500]


def auto_learning_summary(device: dict[str, Any]) -> dict[str, Any]:
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
        "enabled": True,
        "observations": observation_counts,
        "observations_by_state": dict(auto.get("observations", {})),
        "segments": len(segments),
        "segments_by_state": dict(counts),
        "last": last,
        "role": "confidence_weighted_training",
        "weight": AUTO_WEIGHT,
        "feedback": feedback_summary,
        "calibration": dict(auto.get("calibration", {"present_bias": 0.0, "absent_bias": 0.0})),
        "note": "Automatic estimates contribute at 20% × confidence. Human labels always take priority; inferred proportions do not block Apply.",
    }


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
    last = auto.get("last_classification")
    if (
        not last
        or time.time() - last.get("timestamp", 0) > 15
        or last.get("state") not in ("present", "not_present")
    ):
        raise ValueError("No confident automatic classification to give feedback on yet")
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


def _sample_devices(runtime):
    for device_id, device in runtime.data["devices"].items():
        values = _read_energies(runtime, device)
        runtime._live[device_id] = values  # Drop unavailable readings, never carry them forward.
        if not values:
            runtime._auto_runtime.pop(device_id, None)
            device.get("auto", {}).pop("last_classification", None)
            continue
        now = time.time()
        runtime._migrate_device_samples(device)
        result = runtime._classify_auto(device, values)
        runtime._update_auto_state(device_id, device, result, now)
        runtime._record_history_sample(device_id, values, now)
        runtime._schedule_save()


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

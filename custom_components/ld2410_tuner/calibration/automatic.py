"""Fresh learned-result comparisons and guarded application through the normal writer."""

import asyncio
from copy import deepcopy
from time import time

from . import comparison, comparison_jobs, device_io


def policy(runtime, device_id=None):
    """Resolve one global default and an optional device override without copying defaults."""
    config = runtime.data.get("learning_schedule", {})
    default = (
        config.get("auto_apply_scope", "overnight") if config.get("auto_apply", True) else "off"
    )
    device = runtime.data.get("devices", {}).get(device_id, {})
    override = device.get("auto_apply_override", "inherit")
    return {
        "override": override,
        "global": default,
        "effective": default if override == "inherit" else override,
    }


def configure_device(runtime, device_id, mode):
    if mode not in ("inherit", "off", "overnight", "all"):
        raise ValueError("Choose inherit global, off, overnight only, or every Learn")
    device = runtime.data["devices"].get(device_id)
    if device is None:
        raise ValueError("Unknown device")
    if mode == "inherit":
        device.pop("auto_apply_override", None)
    else:
        device["auto_apply_override"] = mode
    runtime._schedule_save()
    return policy(runtime, device_id)


def enabled(runtime, source="automatic", device_id=None):
    mode = policy(runtime, device_id)["effective"]
    if mode == "off":
        return False
    if source == "user":
        return mode == "all"
    config = runtime.data.get("learning_schedule", {})
    return source == "automatic" and config.get("enabled", False)


def decision(patterns, source="automatic"):
    current, candidate = patterns["live"], patterns[source]
    if any(item.get("score") is None for item in (current, candidate)):
        return "Not enough usable occupied and empty evidence to compare both settings."
    if not candidate.get("applicable"):
        return "The learned settings no longer match this device."
    if candidate["error_cost"] >= current["error_cost"] - 1e-9:
        return (
            "The learned settings do not reduce the weighted missed-time and false-active penalty."
        )
    return None


def _guard(runtime, device_id, signature, applied, source):
    if not enabled(runtime, source, device_id):
        raise ValueError("Automatic Apply no longer permits this learning source")
    device = runtime.data["devices"][device_id]
    if not device.get("recording_enabled", True):
        raise ValueError("Recording was disabled for this device")
    _, current = comparison_jobs._context(runtime, device_id)
    expected = signature["patterns"]["live"]["thresholds"]
    if current["patterns"]["live"]["thresholds"] != {**expected, **applied}:
        raise ValueError("Device thresholds changed during automatic Apply")
    current["patterns"]["live"]["thresholds"] = expected
    if current != signature:
        raise ValueError(
            "Labels, timing, saved results or configuration changed; wait for a fresh learn"
        )


async def run(runtime, device_id, learned, report, source="automatic"):
    """Keep one bounded report; application errors must not discard a successful fit."""
    report.update(status="skipped", result_id=learned["id"], source=source)
    if not enabled(runtime, source, device_id):
        report["reason"] = (
            "Automatic Apply is disabled. The result remains available for manual Apply."
        )
        return
    try:
        with device_io.device_operation(runtime, device_id):
            await _compare_and_apply(runtime, device_id, learned, report, source)
    except asyncio.CancelledError:
        report.update(
            status="interrupted",
            reason="Automatic Apply interrupted; check live thresholds before retrying.",
        )
        raise
    except ValueError as error:
        report.update(status="skipped", reason=str(error))
    except Exception as error:
        report.update(status="error", reason=f"Automatic Apply could not finish: {error}")
    finally:
        report["finished_at"] = time()
        runtime._schedule_save()


async def _compare_and_apply(runtime, device_id, learned, report, source):
    context, signature = comparison_jobs._context(runtime, device_id)
    candidate = context["patterns"][source]
    if not candidate or candidate["id"] != learned["id"]:
        raise ValueError("The learned result was replaced; wait for a fresh learn")
    report.update(status="comparing", started_at=time())
    runtime._schedule_save()
    # Stored display scores may use different recordings. Never use them to authorize writes.
    context["patterns"] = {name: context["patterns"][name] for name in ("live", source)}
    view = runtime._history_view(device_id)
    async with runtime._comparison_semaphore:
        measured = await runtime.hass.async_add_executor_job(
            comparison.calculate, view, device_id, context
        )
    report.update(assessment=measured, scorer_version=comparison.SCORER_VERSION)

    def guard(applied):
        _guard(runtime, device_id, signature, applied, source)

    guard({})
    reason = decision(measured["patterns"], source)
    if reason:
        report.update(status="skipped", reason=reason)
        return
    report.update(status="applying")
    runtime._schedule_save()
    outcome = await runtime._apply_validated(
        device_id,
        source,
        learned["id"],
        deepcopy(context["patterns"]["live"]["thresholds"]),
        guard=guard,
    )
    report["writes"] = outcome
    if outcome["skipped"]:
        report.update(
            status="partial",
            reason="Some thresholds could not be confirmed. Check the live values before applying again.",
        )
    else:
        report.update(
            status="applied",
            reason="Lower weighted error on the same recordings; reported thresholds match. Previous settings are saved.",
        )

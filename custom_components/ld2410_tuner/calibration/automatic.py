"""Fresh overnight comparisons and guarded application through the normal writer."""

import asyncio
from copy import deepcopy
from time import time

from . import comparison, comparison_jobs, device_io


def enabled(runtime):
    policy = runtime.data.get("learning_schedule", {})
    return policy.get("enabled", False) and policy.get("auto_apply", True)


def decision(patterns):
    current, candidate = patterns["live"], patterns["automatic"]
    if any(item.get("score") is None for item in (current, candidate)):
        return "Not enough usable occupied and empty evidence to compare both settings."
    if not candidate.get("applicable"):
        return "The learned settings no longer match this device."
    if candidate["error_cost"] >= current["error_cost"] - 1e-9:
        return (
            "The learned settings do not reduce the weighted missed-time and false-active penalty."
        )
    return None


def _guard(runtime, device_id, signature, applied):
    if not enabled(runtime):
        raise ValueError("Automatic Apply or overnight learning was disabled")
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


async def run(runtime, device_id, learned, report):
    """Keep one bounded report; application errors must not discard a successful fit."""
    report.update(status="skipped", result_id=learned["id"])
    if not enabled(runtime):
        report["reason"] = (
            "Automatic Apply is disabled. The result remains available for manual Apply."
        )
        return
    try:
        with device_io.device_operation(runtime, device_id):
            await _compare_and_apply(runtime, device_id, learned, report)
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


async def _compare_and_apply(runtime, device_id, learned, report):
    context, signature = comparison_jobs._context(runtime, device_id)
    candidate = context["patterns"]["automatic"]
    if not candidate or candidate["id"] != learned["id"]:
        raise ValueError("The overnight result was replaced; wait for a fresh learn")
    report.update(status="comparing", started_at=time())
    runtime._schedule_save()
    # Stored display scores may use different recordings. Never use them to authorize writes.
    context["patterns"] = {name: context["patterns"][name] for name in ("live", "automatic")}
    view = runtime._history_view(device_id)
    async with runtime._comparison_semaphore:
        measured = await runtime.hass.async_add_executor_job(
            comparison.calculate, view, device_id, context
        )
    report.update(assessment=measured, scorer_version=comparison.SCORER_VERSION)

    def guard(applied):
        _guard(runtime, device_id, signature, applied)

    guard({})
    reason = decision(measured["patterns"])
    if reason:
        report.update(status="skipped", reason=reason)
        return
    report.update(status="applying")
    runtime._schedule_save()
    outcome = await runtime._apply_validated(
        device_id,
        "automatic",
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

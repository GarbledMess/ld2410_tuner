"""Trim both boundaries of an observed external-positive period before training."""

from ..const import HISTORY_SAMPLE_INTERVAL
from ..history.source_labels import confirm_presence


def observe(runtime, device_id, positive, config, now):
    states = runtime._source_runtime
    period = states.get(device_id)
    if period and now - period["last_seen"] > HISTORY_SAMPLE_INTERVAL * 2:
        states.pop(device_id)
        period = None  # Never bridge telemetry outages or an integration restart.
    if period:
        cutoff = now - config["end_buffer_seconds"]
        confirm_presence(runtime, device_id, period["cursor"], cutoff, round(config["confidence"]))
        period["cursor"] = max(period["cursor"], cutoff)
        period["last_seen"] = now
    if not positive:
        states.pop(device_id, None)
        return False
    if period is None:
        period = {"start": now, "last_seen": now, "cursor": now + config["start_buffer_seconds"]}
        states[device_id] = period
    return now > period["start"] + config["start_buffer_seconds"] + config["end_buffer_seconds"]


def ready(runtime, device_id, config, now):
    period = runtime._source_runtime.get(device_id)
    return bool(
        period
        and now - period["last_seen"] <= HISTORY_SAMPLE_INTERVAL * 2
        and now > period["start"] + config["start_buffer_seconds"] + config["end_buffer_seconds"]
    )

"""Bounded saved recommendations and explicit application selection."""

from copy import deepcopy
from time import time
from uuid import uuid4

from .fitting import METHOD

SLOTS = ("user", "previous", "current", "automatic")


def saved_results(device):
    """Migrate the existing manual preview without duplicating recording data."""
    saved = device.setdefault("learning_results", {})
    if not saved and device.get("last_learning"):
        result = deepcopy(device["last_learning"])
        result.setdefault("id", uuid4().hex)
        result.setdefault("source", "user")
        saved["user"] = result
    return saved


def remember_learning(device, learned, source):
    result = deepcopy(learned)
    result.setdefault("id", uuid4().hex)
    result["source"] = source
    saved_results(device)[source] = result
    if source == "user":
        device["last_learning"] = result
    return result


def selected_result(device, slot, result_id):
    if slot not in SLOTS:
        raise ValueError("Unknown saved result slot")
    result = saved_results(device).get(slot)
    if not result or not result_id or result.get("id") != result_id:
        raise ValueError("Saved result changed; refresh and select it again")
    return deepcopy(result)


def remember_applied(device, learned, entities, before):
    saved = saved_results(device)
    current = saved.get("current")
    thresholds = {key: p["threshold"] for key, p in learned["proposals"].items()}
    if current and current.get("id") == learned.get("id") and before == thresholds:
        return
    saved["previous"] = (
        deepcopy(current)
        if _matches(current, before)
        else {
            "id": uuid4().hex,
            "source": "device",
            "method": METHOD,
            "status": "unmeasured",
            "created_at": time(),
            "entities": dict(entities),
            "configuration": dict(before),
            "proposals": {key: {"threshold": value} for key, value in before.items()},
        }
    )
    saved["current"] = deepcopy(learned)
    saved["current"].setdefault("id", uuid4().hex)
    saved["current"]["applied_at"] = time()


def _matches(result, values):
    return (
        bool(result)
        and {key: proposal["threshold"] for key, proposal in result.get("proposals", {}).items()}
        == values
    )

"""What one overnight item learns from, and whether any of it changed since its last Learn."""

import hashlib
import json
import logging
from typing import NamedTuple

from ..calibration import automatic, service, settings
from ..calibration import thresholds as threshold_domain
from ..calibration.timing_config import read_timing, timing_signature
from ..const import TRAINING_FIELDS
from ..history import recording

_LOGGER = logging.getLogger(__name__)
BASELINE = "nightly_baseline"
FIELDS = ("human", "thresholds", "evidence")
# A Learn with a usable saved result. "insufficient" has none, so it runs until it does.
SETTLED = frozenset({"ok", "tradeoff", "unsafe", "uncertain", "unchanged"})
UNFINISHED_APPLY = frozenset({"partial", "error", "interrupted", "comparing", "applying"})


class Item(NamedTuple):
    """One overnight Learn: a learning set through its first eligible member, or one radar."""

    lead: str
    device: dict
    members: list
    group_ids: list


def read(runtime, item):
    """Digests of the item's inputs; None when unreadable, so it runs and reports why."""
    try:
        signature = _signature(runtime, item)
        thresholds = _human_only(runtime, signature)
        signature["apply"] = {
            key: automatic.policy(runtime, key)["effective"] for key in sorted(thresholds)
        }
        return {
            "human": _digest(signature),
            "thresholds": _digest(thresholds),
            "evidence": _digest(_evidence(runtime, signature["evidence_sources"])),
        }
    except Exception as error:
        _LOGGER.debug("Overnight inputs of %s are unreadable: %s", item.lead, error)
        return None


def _signature(runtime, item):
    from ..rooms import jobs

    if item.group_ids:
        return jobs.set_context(runtime, item.group_ids)[1]
    return _radar_signature(runtime, item.lead)


def _radar_signature(runtime, device_id):
    """A radar outside any learning set, in the shape of a set's signature."""
    device = runtime.data["devices"][device_id]
    entities, thresholds = runtime._threshold_configuration(device_id)
    return {
        device_id: {
            "entities": entities,
            "thresholds": thresholds,
            "timing": timing_signature(read_timing(runtime, device_id)),
            "revision": 0,
            "training": [device.get(field) for field in TRAINING_FIELDS],
            "constraints": threshold_domain.read(runtime, entities),
        },
        "evidence_sources": service._learning_sources(runtime, device_id),
        **settings.signature(runtime.data),
    }


def _human_only(runtime, signature):
    """Take the thresholds out, as automatic Apply writes them, and replace label revisions
    with human ones: retention bumps ``label_revision``, which is evidence, not a person."""
    devices = runtime.data["devices"]
    thresholds = {}
    for key, part in signature.items():
        if isinstance(part, dict) and "thresholds" in part:
            thresholds[key] = part.pop("thresholds")
            part["revision"] = devices.get(key, {}).get("human_label_revision", 0)
    for key, member in signature["evidence_sources"]["members"].items():
        if isinstance(member, dict) and "labels" in member:
            member["labels"] = devices.get(key, {}).get("human_label_revision", 0)
    return thresholds


def _evidence(runtime, sources):
    """New recordings, recorded history rewritten and automatic room labels."""
    devices = runtime.data["devices"]
    sensor = runtime.data.get("room_sensor_revision", {})
    return {
        "radars": {
            key: [
                devices.get(key, {}).get("label_revision", 0),
                recording.evidence_mark(runtime, key),
            ]
            for key in sorted(sources["members"])
        },
        "rooms": {key: sensor.get(key, 0) for key in sorted(sources["rooms"])},
    }


def _digest(value):
    # Stored between restarts, where tuples come back as lists: compare the JSON form.
    text = json.dumps(value, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf8")).hexdigest()


def unchanged(runtime, item, current):
    """Skip only a usable last result whose every input is as it was. Never hides an error."""
    baseline = item.device.get(BASELINE)
    if current is None or not baseline:
        return False
    if any(baseline.get(field) != current[field] for field in FIELDS):
        return False
    devices = runtime.data["devices"]
    return all(_settled(devices.get(key)) for key in item.members or [item.lead])


def _settled(device):
    run = (device or {}).get("nightly_learning") or {}
    saved = (device or {}).get("learning_results", {}).get("automatic", {})
    return (
        run.get("status") in SETTLED
        and run.get("automatic_apply", {}).get("status") not in UNFINISHED_APPLY
        and bool(run.get("result_id"))
        and saved.get("id") == run["result_id"]
    )


def priority(item, current):
    """Human-provided changes first, then the oldest last Learn; the lead id breaks ties."""
    baseline = item.device.get(BASELINE) or {}
    changed = bool(baseline and current) and any(
        baseline.get(field) != current[field] for field in ("human", "thresholds")
    )
    return not changed, baseline.get("day", ""), item.lead


def remember(runtime, item, before, day):
    """After a usable Learn: its inputs as read before it started, so an edit during the fit
    runs again. Only thresholds it applied itself are taken from after."""
    run = item.device.get("nightly_learning", {})
    if run.get("status") not in SETTLED:
        return  # Failures always run; the last usable Learn's day still orders the night.
    if before is None:
        item.device.pop(BASELINE, None)
        return
    baseline = {**before, "day": day}
    if run.get("automatic_apply", {}).get("status") == "applied":
        after = read(runtime, item)
        baseline["thresholds"] = after and after["thresholds"]
    item.device[BASELINE] = baseline

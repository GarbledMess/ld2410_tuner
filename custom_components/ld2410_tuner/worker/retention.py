"""Durable age-retention batches; each child reads only its selected days."""

import datetime
from pathlib import Path

from ..calibration.constants import SAMPLE_SECONDS
from ..const import TRAINING_FIELDS
from ..history import budget, daystore, label_counts, policy
from ..history.label_lookup import _history_label_reader
from . import startup

BATCH_DAYS = 30
BATCH_BYTES = 2 * 1024**2  # Real days vary widely; about 5 desktop CPU-s of the 120 s budget.


def _basis(device):
    return {key: device.get(key) for key in ("history_labels", *TRAINING_FIELDS)}


def _file_bytes(path):
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _paths(store, key, date):
    day = datetime.date.fromisoformat(date)
    return [store._path(key, day, suffix) for suffix in (".day", ".log")]


def _take(store, tokens, max_bytes):
    """Up to a batch of days by count and size; always one, so a large day still progresses."""
    taken, used = [], 0
    for token in tokens[:BATCH_DAYS]:
        used += sum(_file_bytes(path) for path in _paths(store, *token))
        if taken and used > max_bytes:
            break
        taken.append(token)
    return taken


def snapshot(store, data, now, *, max_bytes=BATCH_BYTES):
    progress = data.get("maintenance_progress", {})
    if progress.get("settings", policy.settings(data)) != policy.settings(data):
        progress = {}
    after = tuple(progress.get("after", ("", "")))
    tokens = sorted((key, str(date)) for key in data["devices"] for date in store.days(key))
    remaining = [token for token in tokens if token > after]
    if not remaining:
        remaining = tokens
        progress = {}
    key = remaining[0][0] if remaining else ""
    if progress.get("key") == key and progress.get("basis") != _basis(data["devices"][key]):
        remaining = [token for token in tokens if token[0] >= key]
        progress = {}
    selected = _take(store, [token for token in remaining if token[0] == key], max_bytes)
    paths = [
        str(path.relative_to(store.root)) for token in selected for path in _paths(store, *token)
    ]
    return {
        "_source_paths": paths,
        "batch": {
            "selected": selected,
            "radar_done": not any(token[0] == key for token in remaining[len(selected) :]),
            "done": len(selected) == len(remaining),
            "progress": progress,
            "before": store.size() + budget.storage_bytes(data),
        },
    }


def execute(request):
    from ..history import spans
    from .maintenance import _restore_live

    store = daystore.DayStore(request["root"])
    store.open(request["now"])
    _restore_live(store, request.get("live", {}))
    batch = request["batch"]
    if not batch["selected"]:
        return _empty(request)
    key = batch["selected"][0][0]
    prepared = budget.copy_data(request["data"])
    device = prepared["devices"][key]
    progress = batch["progress"]
    now = progress.get("now", request["now"])
    cuts = {
        (key, date): ranges
        for key, date, ranges in budget.plan(store, prepared, now, policy.settings(prepared))
    }
    planned = budget._Planned(store, None, cuts)
    changes, automatic, human = budget._replacement_days(store, prepared, cuts, planned)
    seconds = progress.get("seconds") if progress.get("key") == key else None
    seconds = label_counts.empty() if seconds is None else seconds
    read = _history_label_reader(device)
    cutoff = max(0, now - policy.settings(prepared)["human_days"] * policy.DAY)
    replacements = {(k, d): v for k, d, v in changes}
    for date in store.days(key):
        day = budget._counted_day(store, key, date, replacements, now)
        if day is not None:
            label_counts._count_native(seconds, spans.day_spans(day), read, cutoff, 1)
    changed = _metadata(prepared, key, seconds, batch, now, changes)
    encoded = {token: value[1] for token, value in planned.replaced.items() if value[1]}
    staged = store.stage(changes, encoded)
    used = batch["before"] + sum(planned.change.values())
    used += budget.storage_bytes(prepared) - budget.storage_bytes(request["data"])
    report = budget._report(
        batch["before"],
        used,
        int(policy.settings(prepared)["max_mib"] * policy.MIB),
        now,
        automatic,
        human,
    )
    report.update(pending=not batch["done"], batched=True)
    return {
        "prepared": prepared,
        "changes": _handoff(staged, Path(request["directory"])),
        "report": report,
        "counts": {},
        "prune": False,
        "guard_keys": [key],
        "metadata_keys": [key] if batch["radar_done"] and changed else [],
    }


def _metadata(prepared, key, seconds, batch, now, changes):
    device = prepared["devices"][key]
    previous = batch["progress"]
    changed = bool(changes) or (previous.get("key") == key and previous.get("changed", False))
    if changes:
        device["label_revision"] = device.get("label_revision", 0) + 1
        device.pop("last_learning", None)
    if batch["radar_done"]:
        if changed:
            device["histograms"] = _stored(seconds)
            device.pop("last_learning", None)
        cutoff = now - policy.settings(prepared)["human_days"] * policy.DAY
        device["history_labels"] = budget.expired_labels(device.get("history_labels", []), cutoff)
        device["history_cleanup"] = {"history_basis": "observed_seconds", "timestamp": now}
    prepared["maintenance_progress"] = {
        "after": batch["selected"][-1],
        "now": now,
        "key": key,
        "seconds": None if batch["radar_done"] else seconds,
        "basis": _basis(device),
        "settings": policy.settings(prepared),
        "changed": changed,
    }
    if batch["done"]:
        prepared.pop("maintenance_progress", None)
        for item in prepared["devices"].values():
            item["history_cleanup"] = {"history_basis": "observed_seconds", "timestamp": now}
        budget._expire_labels(prepared, now - policy.settings(prepared)["human_days"] * policy.DAY)
    return changed


def _handoff(staged, directory):
    return [
        {
            "key": key,
            "date": date.isoformat(),
            "temporary": str(temporary.relative_to(directory)),
            "day": startup.write_day(day, directory, index),
        }
        for index, (key, date, day, temporary) in enumerate(staged)
    ]


def _empty(request):
    prepared = budget._marked_clean(request["data"], request["now"])
    prepared.pop("maintenance_progress", None)
    cutoff = request["now"] - policy.settings(prepared)["human_days"] * policy.DAY
    budget._expire_labels(prepared, cutoff)
    used = request["batch"]["before"]
    report = budget._report(
        used, used, int(policy.settings(prepared)["max_mib"] * policy.MIB), request["now"], 0, 0
    )
    report.update(pending=False, batched=True)
    return {
        "prepared": prepared,
        "changes": [],
        "report": report,
        "counts": {},
        "prune": False,
        "guard_keys": list(prepared["devices"]),
    }


def _stored(seconds):
    return label_counts.stored(
        {
            gate: {
                state: [value / SAMPLE_SECONDS for value in values]
                for state, values in series.items()
            }
            for gate, series in seconds.items()
        }
    )

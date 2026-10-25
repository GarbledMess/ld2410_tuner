"""Retention planning and day compression inside the protected storage child."""

import datetime
from pathlib import Path

from ..history import budget, daystore, policy, summary_cache
from . import startup


def execute(request):
    root, directory = Path(request["root"]), Path(request["directory"])
    store = daystore.DayStore(root)
    now, candidate = request["now"], request["data"]
    store.open(now)
    _restore_live(store, request.get("live", {}))
    target = request.get("target")
    json_bytes, prune = _prune(store, candidate, target, root)
    counts, encoded = {}, {}
    prepared, changes, report = budget.prepare_days(
        store,
        candidate,
        now,
        request.get("clean", False),
        target,
        before=store.size() + json_bytes,
        counts=counts,
        encoded=encoded,
    )
    staged = store.stage(changes, encoded)
    handoff = []
    for index, (key, date, day, temporary) in enumerate(staged):
        handoff.append(
            {
                "key": key,
                "date": date.isoformat(),
                "temporary": str(temporary.relative_to(directory)),
                "day": startup.write_day(day, directory, index),
            }
        )
    return {
        "prepared": prepared,
        "report": report,
        "changes": handoff,
        "prune": prune,
        "counts": counts,
    }


def load(result, directory):
    """Load raw result buffers only; numerical decoding and compression stay in child."""
    directory = Path(directory).resolve()
    changes, staged = [], []
    if len(result["changes"]) > 8192:
        raise ValueError("Storage replacement count budget exceeded")
    paths = []
    for entry in result["changes"]:
        path = (directory / entry["temporary"]).resolve()
        if not path.is_relative_to(directory) or path.suffix != ".trim":
            raise ValueError("Invalid staged storage path")
        paths.append(path)
    startup._budget({"live": [entry["day"] for entry in result["changes"]]}, max_items=8192)
    for entry, path in zip(result["changes"], paths, strict=True):
        day = startup.read_day(directory, entry["day"])
        date = datetime.date.fromisoformat(entry["date"])
        changes.append((entry["key"], date, day))
        staged.append((entry["key"], date, day, path))
    return result["prepared"], changes, result["report"], staged


def _restore_live(store, states):
    for key, state in states.items():
        live = store._live.get(key)
        if live is not None and str(live.date) == state["date"]:
            live.last, live.seen = state["last"], state["seen"]
            live.builder.current = state["current"]
            run = None if state["run"] is None else tuple(state["run"])
            if run is not None:
                _remove_recovery_close(live.builder, run, live.last)
            live.builder.run = run


def _prune(store, candidate, target, root):
    ceiling = int(policy.settings(candidate)["max_mib"] * policy.MIB)
    limit = ceiling if target is None else min(ceiling, target)
    json_bytes = budget.storage_bytes(candidate)
    prune = store.size() + json_bytes > limit
    if prune:
        summary_cache.prune(root, max_bytes=0)
        if summary_cache.disk_bytes(root) and store.size() + json_bytes > limit:
            raise budget.BudgetExceeded("Optional history indexes could not be removed")
    return json_bytes, prune


def _remove_recovery_close(builder, run, last):
    """Undo only the synthetic close added by opening this private live journal."""
    closed = (run[0], last, *run[1:])
    if builder.guesses and builder.guesses[-1] == closed:
        builder.guesses.pop()

"""Retention planning and day compression inside the protected storage child."""

import datetime
import time
from pathlib import Path

from ..history import budget, daystore, policy, summary_cache
from . import diagnostics, startup


def execute(request):
    if "batch" in request:
        from . import retention

        return retention.execute(request)
    root, directory = Path(request["root"]), Path(request["directory"])
    store = daystore.DayStore(root, cache_bytes=0)
    now, candidate = request["now"], request["data"]
    profile = []
    wall, cpu = time.perf_counter(), time.process_time()
    active = "open"

    def measure(stage, starting=False):
        nonlocal wall, cpu, active
        if starting:
            active = stage
            return
        elapsed, used = time.perf_counter(), time.process_time()
        item = {
            "stage": stage,
            "wall_seconds": round(elapsed - wall, 3),
            "cpu_seconds": round(used - cpu, 3),
            "rss_kib": diagnostics._pairs(Path("/proc/self/status")).get("VmRSS"),
        }
        if len(profile) < 32:
            profile.append(item)
            diagnostics.emit("maintenance_stage", **item)
        wall, cpu = elapsed, used

    try:
        measure("entry")
        store.open(now)
        _restore_live(store, request.get("live", {}))
        measure("open")
        target = request.get("target")
        active = "prune"
        json_bytes, prune = _prune(store, candidate, target, root)
        measure("prune")
        counts = {}
        prepared, changes, report = budget.prepare_days(
            store,
            candidate,
            now,
            request.get("clean", False),
            target,
            before=store.size() + json_bytes,
            counts=counts,
            spool=directory / "replacements",
            profile=measure,
        )
        active = "stage"
        staged = store.stage(changes)
        measure("stage")
        handoff = [
            {
                "key": key,
                "date": date.isoformat(),
                "temporary": str(temporary.relative_to(directory)),
                "present": day.present,
            }
            for key, date, day, temporary in staged
        ]
        return {
            "prepared": prepared,
            "report": report,
            "changes": handoff,
            "prune": prune,
            "counts": counts,
            "profile": profile,
        }
    except BaseException as error:
        diagnostics.emit("maintenance_failure", stage=active, error=type(error).__name__)
        try:
            error.add_note(f"Maintenance failed during {active}")
        except MemoryError:
            pass
        raise


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
    legacy = [entry["day"] for entry in result["changes"] if "day" in entry]
    if legacy:
        startup._budget({"live": legacy}, max_items=8192)
    for entry, path in zip(result["changes"], paths, strict=True):
        date = datetime.date.fromisoformat(entry["date"])
        day = (
            startup.read_day(directory, entry["day"])
            if "day" in entry
            else daystore.StagedDay(path, date, entry["present"])
        )
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
        summary_cache.prune(root, max_bytes=0, max_cell_bytes=0, preserve_counts=True)
        if (
            summary_cache.disk_bytes(root, preserve_counts=True)
            and store.size() + json_bytes > limit
        ):
            raise budget.BudgetExceeded("Optional history indexes could not be removed")
    return json_bytes, prune


def _remove_recovery_close(builder, run, last):
    """Undo only the synthetic close added by opening this private live journal."""
    closed = (run[0], last, *run[1:])
    if builder.guesses and builder.guesses[-1] == closed:
        builder.guesses.pop()

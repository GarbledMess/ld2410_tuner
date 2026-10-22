"""Home Assistant independent execution against a detached read-only day store."""

import datetime
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

from ..history import dayfile, daystore, summaries, summary_cache, worker_snapshot
from . import codec, history


class JobHistory:
    """A bounded resident cache shared across every view of a job."""

    def __init__(self):
        self.cache = OrderedDict()
        self.root = "worker-" + uuid.uuid4().hex
        self.files = set()
        self.attribution_guesses = {}
        self.rows = self.guesses = self.raw_bytes = self.file_bytes = 0

    def retain(self, mark, day, budgets):
        counts = _counts(day)
        if any(
            budgets[name] is not None and actual > budgets[name] for name, actual in counts.items()
        ):
            return
        while self.cache and any(
            budgets[name] is not None and getattr(self, _LEDGERS[name]) + actual > budgets[name]
            for name, actual in counts.items()
        ):
            _, previous = self.cache.popitem(last=False)
            for name, actual in _counts(previous).items():
                attribute = _LEDGERS[name]
                setattr(self, attribute, getattr(self, attribute) - actual)
        for name, actual in counts.items():
            attribute = _LEDGERS[name]
            setattr(self, attribute, getattr(self, attribute) + actual)
        self.cache[mark] = day


_LEDGERS = {"max_rows": "rows", "max_guesses": "guesses", "max_raw_bytes": "raw_bytes"}


def _counts(day):
    return {
        "max_rows": len(day.times),
        "max_guesses": len(day.guesses),
        "max_raw_bytes": sum(
            len(column) * getattr(column, "itemsize", 1)
            for column in (day.times, day.ends, *day.columns)
        )
        + len(day.guesses) * history.GUESS_BYTES,
    }


class ReadOnlyDays:
    """Private worker cache with aggregate allocation limits enforced on first read."""

    def __init__(self, request, shared=None):
        if isinstance(request["days"], dict):
            from .manifest import DayEntries

            self.entries = DayEntries(request["days"])
        else:
            self.entries = {
                (item["device_id"], datetime.date.fromisoformat(item["date"])): item
                for item in request["days"]
            }
        self.live_dates = request.get("live_dates", {})
        self.budgets = request["budgets"]
        self.shared = JobHistory() if shared is None else shared
        self.root = self.shared.root

    def days(self, device_id):
        if hasattr(self.entries, "dates"):
            return self.entries.dates(device_id)
        return sorted(date for key, date in self.entries if key == device_id)

    def live_date(self, device_id):
        value = self.live_dates.get(device_id)
        return None if value is None else datetime.date.fromisoformat(value)

    def identity(self, device_id, date):
        # Live inputs have already been copied to private immutable files too. A
        # job-specific root permits bounded derived-result reuse without leaking
        # evidence between requests or ignoring changed physical file identities.
        entry = self.entries.get((device_id, date))
        if entry is None or entry["path"] is None:
            return None
        expected = tuple(map(int, entry["identity"]))
        if history._identity(Path(entry["path"]).stat()) != expected:
            raise history.HistoryRejected("History changed before derived result acquisition")
        return entry["path"], expected, tuple(tuple(span) for span in entry["late"])

    def day(self, device_id, date, until=None):
        key = device_id, date
        entry = self.entries.get(key)
        if entry is None:
            return None
        mark = (
            entry["path"],
            tuple(entry["identity"] or ()),
            date,
            tuple(tuple(span) for span in entry["late"]),
        )
        if mark in self.shared.cache:
            self.shared.cache.move_to_end(mark)
            return self.shared.cache[mark]
        day = self._read(entry, date)
        self.shared.retain(mark, day, self.budgets)
        return day

    def summary(self, device_id, date, until=None):
        """Reuse optional indexes only beside identity-checked immutable source files."""
        day = self.day(device_id, date, until=until)
        if day is None:
            return None
        entry = self.entries[(device_id, date)]
        if not entry.get("persistent", False):
            return summaries.of_day(day)
        path = Path(entry["path"])
        expected = tuple(map(int, entry["identity"]))
        if history._identity(path.stat()) != expected:
            raise history.HistoryRejected("History changed before summary acquisition")
        identity = expected[2:]
        result = summary_cache.load(day, path, identity=identity)
        if result is None:
            result = summaries.of_day(day)
            summary_cache.store(result, path, path.parent.parent, identity=identity)
        if history._identity(path.stat()) != expected:
            raise history.HistoryRejected("History changed during summary acquisition")
        return summaries.remember(day, result)

    def _read(self, entry, date):
        if self.budgets["max_raw_bytes"] <= 0:
            raise history.HistoryRejected("Invalid resident history budget")
        if entry["path"] is None:
            day = dayfile.DayBuilder(*daystore.bounds(date)).day()
        else:
            reference = self._reference(entry)
            day = history.read(
                reference,
                max_rows=self.budgets["max_rows"],
                max_guesses=self.budgets["max_guesses"],
                max_raw_bytes=history.DAY_BYTES,
                max_compressed_bytes=None,
            )
        late = [dayfile.GuessSpan(*span) for span in entry["late"]]
        if (
            self.budgets["max_guesses"] is not None
            and len(late) + len(day.guesses) > self.budgets["max_guesses"]
        ):
            raise history.HistoryRejected("Aggregate late guess budget exceeded")
        return dayfile.splice(day, late)

    def _reference(self, entry):
        physical = entry["path"], tuple(entry["identity"])
        if self.budgets["max_file_bytes"] is not None and physical not in self.shared.files:
            self.shared.file_bytes += int(entry["identity"][3])
            self.shared.files.add(physical)
        if (
            self.budgets["max_file_bytes"] is not None
            and self.shared.file_bytes > self.budgets["max_file_bytes"]
        ):
            raise history.HistoryRejected("Aggregate history file budget exceeded")
        reference = worker_snapshot.FileReference(
            Path(entry["path"]), tuple(map(int, entry["identity"]))
        )
        return reference


def restore(request, shared=None):
    """Rebuild only the pure runtime surface consumed by numerical algorithms."""
    shared = JobHistory() if shared is None else shared
    view = SimpleNamespace(
        data=codec.decode(request["data"]),
        hass=None,
        _days=ReadOnlyDays(request, shared),
        _attribution_guesses=shared.attribution_guesses,
    )
    for key, value in request.get("attributes", {}).items():
        if key not in (
            "_history_until",
            "_snapshot_history_start",
            "_physical_history_start",
            "_attribution_since",
            "_attribution_until",
        ):
            raise ValueError("Unsupported view attribute")
        setattr(view, key, value)
    return view


def _learn(view, parameters):
    from ..calibration import event_fitting, settings

    entities, current = parameters["entities"], parameters["current"]
    constraints = parameters.get("constraints")
    learned = event_fitting.fit(
        view,
        parameters["device_id"],
        list(entities),
        current,
        parameters.get("timing"),
        settings.automatic_weight(view.data),
        constraints=constraints,
    )
    learned.update(settings.signature(view.data))
    learned.update(
        configuration=current,
        entities=entities,
        threshold_constraints=constraints,
        created_at=time.time(),
    )
    return learned


def execute(request):
    """Run an allowlisted complete job, never a named Python callable."""
    if "manifest" in request:
        from .manifest import expand

        request = expand(request)
    operation = request.get("operation")
    if operation not in (
        "learn",
        "comparison",
        "room_fit",
        "room_assess",
        "room_compare",
        "seen_by",
        "references",
        "recount",
        "charts",
        "export_days",
        "recording_rows",
    ):
        raise ValueError("Unsupported worker operation")
    parameters = codec.decode(request.get("parameters", request))
    shared = JobHistory()
    views = (
        {key: restore(value, shared) for key, value in request["views"].items()}
        if "views" in request
        else {request["device_id"]: restore(request)}
    )
    if operation == "learn":
        return _learn(views[parameters["device_id"]], parameters)
    if operation == "comparison":
        from ..calibration.comparison import calculate

        return calculate(
            views[parameters["device_id"]], parameters["device_id"], parameters["context"]
        )
    return _other(operation, views, parameters)


def _members(items, views):
    return {key: {**item, "view": views[item["view_key"]]} for key, item in items.items()}


def _other(operation, views, parameters):
    if operation == "recording_rows":
        from .recordings import calculate, pack_page

        return pack_page(calculate(views[parameters["device_id"]], **parameters))
    if operation in ("charts", "export_days"):
        from .display import calculate

        return calculate(operation, views[parameters["device_id"]], parameters)
    if operation.startswith("room_"):
        return _room(operation, views, parameters)
    if operation == "seen_by":
        from ..rooms.seen_by import build

        return build(views, parameters["definitions"], parameters["group_id"])
    if operation == "references":
        return _references(views, parameters["jobs"])
    from .recount import calculate

    return calculate(views[parameters["device_id"]], **parameters)


def _references(views, jobs):
    from ..presence import reference_training

    results = []
    for job in jobs:
        view = views[job["view_key"]]
        device = view.data["devices"][job["device_id"]]
        cutoff = reference_training.rebuild_human(view, device)
        periods = device["reference_profile"]["periods"]
        results.append(
            {
                "cutoff": cutoff,
                "periods": [item for item in periods if reference_training.owned(item, cutoff)],
            }
        )
    return results


def _room(operation, views, parameters):
    from ..rooms import adaptive, assessment, fitting

    start, end = parameters["start"], parameters["end"]
    if operation == "room_assess":
        return assessment.calculate(
            _members(parameters["members"], views), start, end, parameters.get("room")
        )
    members = {key: _members(items, views) for key, items in parameters["members"].items()}
    rooms = parameters["rooms"]
    if operation == "room_fit":
        contexts = adaptive.collect_contexts(members, start, end, rooms)
        return fitting.fit_set(
            members, start, end, rooms, parameters["weight"], quality_contexts=contexts
        )
    return {
        key: _compare(items, parameters["thresholds"], start, end, rooms[key])
        for key, items in members.items()
    }


def _compare(members, thresholds, start, end, room):
    from ..rooms import adaptive, fitting
    from ..rooms.objective import Objective

    quality = adaptive.models(
        adaptive.collect_contexts({"room": members}, start, end, {"room": room} if room else {})
    )
    objective = Objective(members, start, end, room, quality=quality)
    current = fitting.measure(members, objective.current, start, end, room, quality=quality)
    candidate = fitting.measure(members, thresholds, start, end, room, quality=quality)
    return {
        "live": {**current["room"], "error_cost": objective.costs(objective.current)[0]},
        "candidate": {**candidate["room"], "error_cost": objective.costs(thresholds)[0]},
    }

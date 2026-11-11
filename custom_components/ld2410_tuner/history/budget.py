"""Pure, bounded-file planning: age cleanup first, automatic history before human."""

import json
import math
import tempfile
from bisect import bisect_left, bisect_right
from collections import Counter
from hashlib import blake2b
from pathlib import Path

from ..const import STORAGE_KEY, STORAGE_MINOR_VERSION, STORAGE_VERSION
from . import guesses, pace
from .label_lookup import _history_label_reader
from .policy import DAY, MIB, settings


class BudgetExceeded(ValueError):
    """The protected metadata alone cannot fit the requested ceiling."""


def copy_data(data, max_nodes=1024 * 1024, max_depth=20):
    """A consistent snapshot of the integration's JSON data (history lives in day files).

    Containers (dict, list, tuple) are copied; anything else is shared, as immutable JSON
    values are. A save takes it on the event loop, so this is the only walk there: about five
    times faster than ``deepcopy``, bounded like the storage check (``worker.storage_values``)
    that the copy then gets in the executor."""
    nodes = 0

    def copy(value, depth):
        nonlocal nodes
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            raise ValueError("Value node or depth budget exceeded")
        kind = type(value)
        if kind is dict:
            nodes += len(value)
            return {key: copy(item, depth + 1) for key, item in value.items()}
        if kind is list:
            return [copy(item, depth + 1) for item in value]
        if kind is tuple:
            return tuple(copy(item, depth + 1) for item in value)
        return value

    return copy(data, 0)


# TunerStore uses this same standard JSON encoder and HA envelope.
def storage_bytes(data):
    """The size of the saved JSON store, as written (ASCII, so characters are bytes)."""
    envelope = {
        "version": STORAGE_VERSION,
        "minor_version": STORAGE_MINOR_VERSION,
        "key": STORAGE_KEY,
        "data": data,
    }
    return sum(map(len, json.JSONEncoder(indent=2).iterencode(envelope)))


def _finished_dates(store, key):
    from . import daystore

    last = store.last(key)
    live = daystore.date_of(last) if last is not None else None
    return [date for date in store.days(key) if date != live]


def plan(store, data, now, policy, target=None, spool=None):
    """Plan finished-day range cuts; live days are immutable to maintenance. Each day's
    ranges are kept until the day or its labels change (``_retention``), so an hourly plan
    reads no unchanged day."""
    from .cleanup import _cut_union

    cuts = {}
    for key, date, _bounds, ranges in _finished_retention(store, data, persist=spool is not None):
        expired = _expired(ranges, now, policy)
        if expired:
            cuts[key, date] = _cut_union(expired)
    if target is not None:
        _ceiling_plan(store, data, cuts, target, spool=spool)
    return [(key, date, ranges) for (key, date), ranges in cuts.items()]


def _finished_retention(store, data, *, persist=False):
    """``(key, date, bounds, ranges)`` of every finished day, from ``_retention``."""
    for key, device in data.get("devices", {}).items():
        for date in _finished_dates(store, key):
            pace.rest()
            yield key, date, *_retention(store, key, date, device, persist=persist)


def _expired(ranges, now, policy):
    """The parts of ``ranges`` older than their cutoff that still hold something: time
    already cut is a gap, and cutting it again would rewrite the day for nothing."""
    found = []
    for low, high, human, confidence, earliest in ranges:
        cutoff = min(high, _cutoff(now, human, confidence, policy))
        if low < cutoff and earliest is not None and earliest < cutoff:
            found.append((low, cutoff))
    return found


def _retention(store, key, date, device, *, persist=False):
    """The finished ``date``'s recorded bounds and its ranges ``(low, high, human, confidence,
    earliest)``, ``earliest`` being the first time a reading or guess is held in the range
    (None: nothing left there). Kept while the day and its labels are unchanged."""
    from . import day_results, daystore, maintenance_cache

    read = _history_label_reader(device)
    labels = day_results.fingerprint(read, *daystore.bounds(date))

    def derive():
        day = store.day(key, date)
        if day is None:
            return None, ()
        bounds = (day.times[0], day.ends[-1]) if len(day.times) else None
        return bounds, tuple(_held_ranges(day, device))

    def persisted():
        if not persist:
            return derive()
        identity = store.identity(key, date)
        if identity is None:
            return derive()
        path = store._path(key, date, ".day")
        signature = identity[-2:], labels
        found = maintenance_cache.load_retention(path, signature)
        if found is not None:
            return found
        value = derive()
        maintenance_cache.save_retention(path, signature, value)
        return value

    return day_results.remembered(store, key, date, "retention", (labels,), persisted)


def _held_ranges(day, device):
    from bisect import bisect_left, bisect_right

    guess_ends = [run[1] for run in day.guesses]
    for low, high, human, confidence in _day_ranges(day, device):
        held = []
        first = bisect_right(day.ends, low)
        if first < bisect_left(day.times, high):
            held.append(max(day.times[first], low))
        index = bisect_right(guess_ends, low)
        if index < len(day.guesses) and day.guesses[index][0] < high:
            held.append(max(day.guesses[index][0], low))
        yield low, high, human, confidence, min(held, default=None)


def _cutoff(now, human, confidence, policy):
    if human:
        return now - policy["human_days"] * DAY
    thin, automatic, minimum = (
        policy[k] for k in ("thin_after_days", "automatic_days", "minimum_confidence")
    )
    fraction = max(0, (confidence - minimum) / (100 - minimum)) if minimum < 100 else 0
    age = thin + fraction * (automatic - thin)
    if confidence >= 100:
        age = automatic
    return now - age * DAY


def _day_ranges(day, device):
    read = _history_label_reader(device)
    runs = guesses.Guesses(day.guesses)
    edges = sorted(
        {
            day.start,
            day.end,
            *(t for t in read.edges if day.start < t < day.end),
            *(t for run in day.guesses for t in run[:2] if day.start < t < day.end),
        }
    )
    pieces = list(zip(edges, edges[1:], strict=False))
    confidences = runs.percents([low for low, _high in pieces])  # One walk over the runs.
    for (low, high), confidence in zip(pieces, confidences, strict=True):
        label = read(low)
        human = label in ("present", "not_present")
        yield low, high, human, confidence if label != "unknown" else 0


class _Planned:
    """The planned total size, kept per cut day: a new cut re-encodes only its own day, not
    every day cut so far. Each day's latest replacement is kept with its encoding (``replaced``)
    for the cleanup to stage; the original counts as the file it is."""

    def __init__(self, store, data, cuts, spool=None):
        self.store = store
        self.spool = spool
        self.base = None if data is None else store.size() + storage_bytes(data)
        self.original, self.change, self.replaced = {}, {}, {}
        for token, ranges in cuts.items():
            self.cut(token, ranges)

    def cut(self, token, ranges):
        from . import dayfile
        from .cleanup import cut

        pace.rest()
        day = self.store.day(*token)
        if token not in self.original:
            self.original[token] = self._stored_bytes(token, day)
        updated = cut(day, ranges)
        present = bool(len(updated.times) or updated.guesses)
        encoded = dayfile.encode_day(updated) if present else b""
        if self.spool is None:
            self.replaced[token] = updated, encoded
        else:
            from .daystore import StagedDay

            Path(self.spool).mkdir(parents=True, exist_ok=True)
            name = blake2b(repr(token).encode(), digest_size=16).hexdigest() + ".replacement"
            path = Path(self.spool) / name
            with tempfile.NamedTemporaryFile(
                dir=self.spool, suffix=".replacementtmp", delete=False
            ) as out:
                temporary = Path(out.name)
                out.write(encoded)
            temporary.replace(path)
            self.replaced[token] = StagedDay(path, token[1], present), None
        self.change[token] = len(encoded) - self.original[token]

    def _stored_bytes(self, token, day):
        from . import dayfile

        try:
            return self.store._path(*token, ".day").stat().st_size
        except OSError:
            return len(dayfile.encode_day(day))

    @property
    def total(self):
        return self.base + sum(self.change.values())

    def total_with(self, data):
        return self.store.size() + storage_bytes(data) + sum(self.change.values())


def _day_candidates(store, data, *, persist=False):
    for key, date, bounds, ranges in _finished_retention(store, data, persist=persist):
        if bounds is None:
            continue
        for low, high, human, confidence, _earliest in ranges:
            low, high = max(low, bounds[0]), min(high, bounds[1])
            if high > low:
                yield human, confidence, low, key, date, high


def _ceiling_plan(store, data, cuts, target, spool=None):
    from .cleanup import _cut_union

    candidates = sorted(_day_candidates(store, data, persist=spool is not None))

    def selected(count):
        chosen = {token: list(ranges) for token, ranges in cuts.items()}
        for _human, _confidence, low, key, date, high in candidates[:count]:
            chosen.setdefault((key, date), []).append((low, high))
        return {token: _cut_union(ranges) for token, ranges in chosen.items()}

    base = _Planned(store, data, cuts, spool)
    if base.total <= target or not candidates:
        return cuts
    low, high = 1, len(candidates)
    while low < high:
        middle = (low + high) // 2
        planned = _Planned(store, data, selected(middle), spool)
        if planned.total <= target:
            high = middle
        else:
            low = middle + 1
    cuts.clear()
    cuts.update(selected(low))
    return cuts


def _replacement_days(store, data, cuts, planned):
    changes, automatic, human = [], 0.0, 0.0
    for (key, date), ranges in cuts.items():
        pace.rest()
        removed = _removed_seconds(store.day(key, date), ranges, data["devices"][key])
        automatic += removed[0]
        human += removed[1]
        changes.append((key, date, planned.replaced[key, date][0]))
    return changes, automatic, human


def _bounded_days(
    store, data, cuts, now, target, counts=None, encoded=None, spool=None, profile=None
):
    while True:
        _stage(profile, "replacements", True)
        planned = _Planned(store, None, cuts, spool)
        changes, automatic, human = _replacement_days(store, data, cuts, planned)
        _stage(profile, "replacements")
        prepared = copy_data(data)
        _stage(profile, "histograms", True)
        _day_metadata(store, prepared, changes, now, counts)
        _stage(profile, "histograms")
        _stage(profile, "size", True)
        used = planned.total_with(prepared)
        _stage(profile, "size")
        if used <= target:
            _keep_encodings(encoded, spool, planned)
            return prepared, changes, automatic, human, used
        previous = {key: list(ranges) for key, ranges in cuts.items()}
        _stage(profile, "ceiling", True)
        _ceiling_plan(store, prepared, cuts, target, spool=spool)
        _stage(profile, "ceiling")
        data = prepared
        if cuts == previous:
            raise BudgetExceeded(
                "The size limit is below the remaining settings, summaries and saved results. Increase the limit or clear unused devices; no oversized file was written."
            )


def _stage(profile, name, starting=False):
    if profile is not None:
        profile(name, starting)


def _keep_encodings(encoded, spool, planned):
    if encoded is not None and spool is None:
        encoded.update(
            (token, replaced[1]) for token, replaced in planned.replaced.items() if replaced[1]
        )


def expired_labels(labels, cutoff):
    """``labels`` without the time before ``cutoff`` (the human retention): older labels go,
    one spanning it starts there. Nothing older can be learned from or shown."""
    return [
        {**label, "start": max(label["start"], cutoff)} if label["start"] < cutoff else label
        for label in labels
        if label["end"] > cutoff
    ]


def _expire_labels(data, cutoff):
    for device in data.get("devices", {}).values():
        if "history_labels" in device:
            device["history_labels"] = expired_labels(device["history_labels"], cutoff)


def prepare_days(
    store,
    data,
    now,
    clean=False,
    target=None,
    before=None,
    counts=None,
    encoded=None,
    spool=None,
    profile=None,
):
    """Prepare immutable replacements and a seconds report; mutate no files or live state.
    ``before`` is the current total size when the caller measured it already. A save that
    cleans nothing and fits returns ``data`` as it is (callers pass their own snapshot).
    ``counts`` (a dict) receives each changed radar's labelled counts behind its new
    histograms, as ``label_counts.adopt`` keeps them; ``encoded`` (a dict) each replacement
    day's encoding, for ``DayStore.stage``."""
    ceiling = int(settings(data)["max_mib"] * MIB)
    target = ceiling if target is None else min(ceiling, target)
    before = store.size() + storage_bytes(data) if before is None else before
    if not clean and before <= target:
        return data, [], _report(before, before, ceiling, now, 0.0, 0.0)
    if clean:
        data = _marked_clean(data, now)
    # Cuts only shrink the store: the ceiling needs planning only when it is exceeded already.
    _stage(profile, "retention", True)
    planned = plan(
        store, data, now, settings(data), target=target if before > target else None, spool=spool
    )
    _stage(profile, "retention")
    cuts = {(key, date): ranges for key, date, ranges in planned}
    prepared, changes, automatic, human, used = _bounded_days(
        store, data, cuts, now, target, counts, encoded, spool, profile
    )
    if clean:  # After planning: the removed time is reported under the labels it had.
        _expire_labels(prepared, now - settings(data)["human_days"] * DAY)
    return prepared, changes, _report(before, used, ceiling, now, automatic, human)


def _marked_clean(data, now):
    data = copy_data(data)
    for device in data.get("devices", {}).values():
        device["history_cleanup"] = {"history_basis": "observed_seconds", "timestamp": now}
    return data


def _report(before, used, ceiling, now, automatic, human):
    return {
        "before_bytes": before,
        "used_bytes": used,
        "limit_bytes": ceiling,
        "timestamp": now,
        "removed_automatic": automatic,
        "removed_human": human,
        "removed_automatic_seconds": automatic,
        "removed_human_seconds": human,
        "history_basis": "observed_seconds",
    }


def _day_seconds(day, device):
    return _seconds_within(day, device, ((-math.inf, math.inf),))


def _removed_seconds(day, ranges, device):
    """``_day_seconds`` of what cutting ``ranges`` out of ``day`` removes: ``cleanup.cut``
    keeps the rest of the day exactly, so only the time inside the ranges is read."""
    from .cleanup import _cut_union

    return _seconds_within(day, device, _cut_union(ranges))


def _seconds_within(day, device, windows):
    """Observed ``[automatic, human-labelled]`` seconds of ``day`` within ``windows``."""
    from . import spans

    read = _history_label_reader(device)
    totals = [0.0, 0.0]
    for low, high in windows:
        for span in spans.day_spans(day, low, high):
            if not any(value is not None for value in span.values):
                continue
            edges = [span.start, *(t for t in read.edges if span.start < t < span.end), span.end]
            for start, end in zip(edges, edges[1:], strict=False):
                totals[read(start) in ("present", "not_present")] += end - start
    return totals


def _day_histograms(store, key, device, replacements, now, cutoff, counted=None):
    """The radar's histograms after the planned replacements, as a label edit stores them.
    ``counted`` (a dict) receives the counts behind them: the finished days' and the live
    day's apart, with the dates counted (``label_counts.adopt``)."""
    from ..calibration.constants import SAMPLE_SECONDS
    from . import label_counts

    raw, finished, live, dates = label_counts.empty(), label_counts.empty(), None, []
    read = _history_label_reader(device)
    live_date = store.live_date(key)
    for date in store.days(key):
        pace.rest()
        part = _day_part(store, key, date, replacements, now, read, cutoff, live_date)
        if part is None:
            continue
        label_counts._combine(raw, part)
        if date == live_date:
            live = part
        else:
            label_counts._combine(finished, part)
            dates.append(date.isoformat())
    if counted is not None:
        counted.update(
            seconds=finished,
            live=live,
            inert=[],
            dates=dates,
            live_date=None if live_date is None else live_date.isoformat(),
            cutoff=cutoff,
        )
    return label_counts.stored(
        {
            gate: {
                state: [value / SAMPLE_SECONDS for value in counts]
                for state, counts in series.items()
            }
            for gate, series in raw.items()
        }
    )


def _day_part(store, key, date, replacements, now, read, cutoff, live_date):
    from . import label_counts, maintenance_cache, spans

    signature = _day_signature(store, key, date, replacements, read, cutoff, live_date)
    path = store._path(key, date, ".day")
    part = maintenance_cache.load(path, signature) if signature is not None else None
    if part is not None:
        return part
    day = _counted_day(store, key, date, replacements, now)
    if day is None:
        return None
    part = _uniform_day_counts(day, read, cutoff)
    if part is None:
        part = label_counts.empty()
        label_counts._count_native(part, spans.day_spans(day), read, cutoff, 1)
    if signature is not None:
        maintenance_cache.save(path, signature, part)
    return part


def _day_signature(store, key, date, replacements, read, cutoff, live_date):
    from . import day_results, daystore

    if date == live_date or (key, date) in replacements:
        return None
    identity = store.identity(key, date)
    if identity is None:
        return None
    low, high = daystore.bounds(date)
    return (
        identity[-2:],
        day_results.fingerprint(read, low, high),
        min(max(cutoff, low), high),
    )


def _uniform_day_counts(day, read, cutoff):
    """Count a fully labelled, constant-duration byte day in C without per-row spans."""
    from ..const import HISTOGRAM_BINS, HISTORY_KEYS
    from . import label_counts

    count = len(day.times)
    if not count:
        return label_counts.empty()
    low, high = day.times[0], day.ends[-1]
    if cutoff > low or bisect_right(read.edges, low) < bisect_left(read.edges, high):
        return None
    state = read(low)
    if state not in ("present", "not_present"):
        return label_counts.empty()
    duration = day.ends[0] - low
    if not all(end - start == duration for start, end in zip(day.times, day.ends, strict=True)):
        return None
    if not _byte_columns(day, count, len(HISTORY_KEYS)):
        return None
    result = label_counts.empty()
    for gate, column in zip(HISTORY_KEYS, day.columns, strict=True):
        histogram = _column_counts(column, duration, HISTOGRAM_BINS)
        if histogram is None:
            return None
        result[gate][state] = histogram
    return result


def _byte_columns(day, count, expected):
    return len(day.columns) == expected and all(
        isinstance(column, bytes) and len(column) == count for column in day.columns
    )


def _column_counts(column, duration, bins):
    frequencies = Counter(column)
    if any(value >= bins and value != 255 for value in frequencies):
        return None
    return [frequencies.get(value, 0) * duration for value in range(bins)]


def _counted_day(store, key, date, replacements, now):
    """The day as the cleanup leaves it: its replacement, or as it is; None when it has no
    recorded time left (an emptied day's file goes)."""
    day = replacements.get((key, date))
    if day is None:
        return store.day(key, date, until=now)
    from .daystore import StagedDay

    if isinstance(day, StagedDay):
        day = day.load()
    return day if len(day.times) or day.guesses else None


def _day_metadata(store, prepared, changes, now, counts=None):
    replacements = {(key, date): day for key, date, day in changes}
    if counts is not None:
        counts.clear()  # A ceiling plan's next attempt counts again.
    for key in {key for key, _date, _day in changes}:
        device = prepared["devices"][key]
        cutoff = max(0, now - settings(prepared)["human_days"] * DAY)
        counted = None if counts is None else counts.setdefault(key, {})
        device["histograms"] = _day_histograms(
            store, key, device, replacements, now, cutoff, counted
        )
        device["label_revision"] = device.get("label_revision", 0) + 1
        device.pop("last_learning", None)

"""Pure, bounded-file planning: age cleanup first, automatic history before human."""

import json
from copy import deepcopy

from ..const import STORAGE_KEY, STORAGE_MINOR_VERSION, STORAGE_VERSION
from . import guesses, pace
from .label_lookup import _history_label_reader
from .policy import DAY, MIB, settings


class BudgetExceeded(ValueError):
    """The protected metadata alone cannot fit the requested ceiling."""


def copy_data(data):
    """A consistent snapshot of the integration's JSON data (history lives in day files)."""
    return deepcopy(data)


# TunerStore uses this same standard JSON encoder and HA envelope.
def storage_bytes(data):
    """The size of the saved JSON store, as written (ASCII, so characters are bytes)."""
    envelope = {
        "version": STORAGE_VERSION,
        "minor_version": STORAGE_MINOR_VERSION,
        "key": STORAGE_KEY,
        "data": data,
    }
    return len(json.dumps(envelope, indent=2))


def _finished_dates(store, key):
    from . import daystore

    last = store.last(key)
    live = daystore.date_of(last) if last is not None else None
    return [date for date in store.days(key) if date != live]


def plan(store, data, now, policy, target=None):
    """Plan finished-day range cuts; live days are immutable to maintenance. Each day's
    ranges are kept until the day or its labels change (``_retention``), so an hourly plan
    reads no unchanged day."""
    from .cleanup import _cut_union

    cuts = {}
    for key, date, _bounds, ranges in _finished_retention(store, data):
        expired = _expired(ranges, now, policy)
        if expired:
            cuts[key, date] = _cut_union(expired)
    if target is not None:
        _ceiling_plan(store, data, cuts, target)
    return [(key, date, ranges) for (key, date), ranges in cuts.items()]


def _finished_retention(store, data):
    """``(key, date, bounds, ranges)`` of every finished day, from ``_retention``."""
    for key, device in data.get("devices", {}).items():
        for date in _finished_dates(store, key):
            pace.rest()
            yield key, date, *_retention(store, key, date, device)


def _expired(ranges, now, policy):
    """The parts of ``ranges`` older than their cutoff that still hold something: time
    already cut is a gap, and cutting it again would rewrite the day for nothing."""
    found = []
    for low, high, human, confidence, earliest in ranges:
        cutoff = min(high, _cutoff(now, human, confidence, policy))
        if low < cutoff and earliest is not None and earliest < cutoff:
            found.append((low, cutoff))
    return found


def _retention(store, key, date, device):
    """The finished ``date``'s recorded bounds and its ranges ``(low, high, human, confidence,
    earliest)``, ``earliest`` being the first time a reading or guess is held in the range
    (None: nothing left there). Kept while the day and its labels are unchanged."""
    from . import day_results, daystore

    read = _history_label_reader(device)
    labels = day_results.fingerprint(read, *daystore.bounds(date))

    def derive():
        day = store.day(key, date)
        if day is None:
            return None, ()
        bounds = (day.times[0], day.ends[-1]) if len(day.times) else None
        return bounds, tuple(_held_ranges(day, device))

    return day_results.remembered(store, key, date, "retention", (labels,), derive)


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
    """The planned total size, kept per cut day: a new cut re-encodes only its own day (the
    original once), not every day cut so far."""

    def __init__(self, store, data, cuts):
        self.store, self.base = store, store.size() + storage_bytes(data)
        self.original, self.change = {}, {}
        for token, ranges in cuts.items():
            self.cut(token, ranges)

    def cut(self, token, ranges):
        from . import dayfile
        from .cleanup import cut

        pace.rest()
        day = self.store.day(*token)
        if token not in self.original:
            self.original[token] = len(dayfile.encode_day(day))
        updated = cut(day, ranges)
        size = len(dayfile.encode_day(updated)) if len(updated.times) or updated.guesses else 0
        self.change[token] = size - self.original[token]

    @property
    def total(self):
        return self.base + sum(self.change.values())


def _planned_bytes(store, data, cuts):
    return _Planned(store, data, cuts).total


def _day_candidates(store, data):
    for key, date, bounds, ranges in _finished_retention(store, data):
        if bounds is None:
            continue
        for low, high, human, confidence, _earliest in ranges:
            low, high = max(low, bounds[0]), min(high, bounds[1])
            if high > low:
                yield human, confidence, low, key, date, high


def _ceiling_plan(store, data, cuts, target):
    from .cleanup import _cut_union

    planned = _Planned(store, data, cuts)
    for _human, _confidence, low, key, date, high in sorted(_day_candidates(store, data)):
        if planned.total <= target:
            break
        token = key, date
        cuts[token] = _cut_union([*cuts.get(token, []), (low, high)])
        planned.cut(token, cuts[token])
    return cuts


def _replacement_days(store, data, cuts):
    from .cleanup import cut

    changes, automatic, human = [], 0.0, 0.0
    for (key, date), ranges in cuts.items():
        pace.rest()
        original = store.day(key, date)
        updated = cut(original, ranges)
        old = _day_seconds(original, data["devices"][key])
        new = _day_seconds(updated, data["devices"][key])
        automatic += old[0] - new[0]
        human += old[1] - new[1]
        changes.append((key, date, updated))
    return changes, automatic, human


def _bounded_days(store, data, cuts, now, target):
    while True:
        changes, automatic, human = _replacement_days(store, data, cuts)
        prepared = copy_data(data)
        _day_metadata(store, prepared, changes, now)
        used = _planned_bytes(store, prepared, cuts)
        if used <= target:
            return prepared, changes, automatic, human, used
        previous = {key: list(ranges) for key, ranges in cuts.items()}
        _ceiling_plan(store, prepared, cuts, target)
        if cuts == previous:
            raise BudgetExceeded(
                "The size limit is below the remaining settings, summaries and saved results. Increase the limit or clear unused devices; no oversized file was written."
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


def prepare_days(store, data, now, clean=False, target=None, before=None):
    """Prepare immutable replacements and a seconds report; mutate no files or live state.
    ``before`` is the current total size when the caller measured it already. A save that
    cleans nothing and fits returns ``data`` as it is (callers pass their own snapshot)."""
    ceiling = int(settings(data)["max_mib"] * MIB)
    target = ceiling if target is None else min(ceiling, target)
    before = store.size() + storage_bytes(data) if before is None else before
    if not clean and before <= target:
        return data, [], _report(before, before, ceiling, now, 0.0, 0.0)
    if clean:
        data = _marked_clean(data, now)
    # Cuts only shrink the store: the ceiling needs planning only when it is exceeded already.
    planned = plan(store, data, now, settings(data), target=target if before > target else None)
    cuts = {(key, date): ranges for key, date, ranges in planned}
    prepared, changes, automatic, human, used = _bounded_days(store, data, cuts, now, target)
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
    from . import spans

    read = _history_label_reader(device)
    totals = [0.0, 0.0]
    for span in spans.day_spans(day):
        if not any(value is not None for value in span.values):
            continue
        edges = [span.start, *(t for t in read.edges if span.start < t < span.end), span.end]
        for low, high in zip(edges, edges[1:], strict=False):
            totals[read(low) in ("present", "not_present")] += high - low
    return totals


def _day_histograms(store, key, device, replacements, now, cutoff):
    """The radar's histograms after the planned replacements, as a label edit stores them."""
    from ..calibration.constants import SAMPLE_SECONDS
    from . import label_counts, spans

    raw = label_counts.empty()
    read = _history_label_reader(device)
    for date in store.days(key):
        pace.rest()
        day = replacements.get((key, date))
        if day is None:
            day = store.day(key, date, until=now)
        if day is not None:
            label_counts._count_native(raw, spans.day_spans(day), read, cutoff, 1)
    return label_counts.stored(
        {
            gate: {
                state: [value / SAMPLE_SECONDS for value in counts]
                for state, counts in series.items()
            }
            for gate, series in raw.items()
        }
    )


def _day_metadata(store, prepared, changes, now):
    replacements = {(key, date): day for key, date, day in changes}
    for key in {key for key, _date, _day in changes}:
        device = prepared["devices"][key]
        cutoff = max(0, now - settings(prepared)["human_days"] * DAY)
        device["histograms"] = _day_histograms(store, key, device, replacements, now, cutoff)
        device["label_revision"] = device.get("label_revision", 0) + 1
        device.pop("last_learning", None)

"""Physical energy spans, with explicit event coverage and legacy sampled uncertainty."""

import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass

from . import daystore


@dataclass(frozen=True, slots=True)
class Span:
    start: float
    end: float
    values: tuple
    mode: str


def has_events(view, device_id, *, start=-math.inf, end=math.inf):
    start = max(start, getattr(view, "_physical_history_start", -math.inf))
    end = min(end, getattr(view, "_attribution_until", math.inf))
    if end <= start:
        return False
    return _store_days(view, device_id, start, end) != []


def _store_days(view, device_id, start, end):
    days = getattr(view, "_days", None)
    if days is None:
        return []
    return [
        date
        for date in days.days(device_id)
        if daystore.bounds(date)[1] > start and daystore.bounds(date)[0] < end
    ]


def _stored(view, device_id, start, end):
    """Read selected context ranges by bisecting each day's immutable row columns."""
    for day, selected in _selected_days(view, device_id, start, end):
        for low, high in selected:
            yield from day_spans(day, low, high)


def _selected_days(view, device_id, start, end):
    until = getattr(view, "_history_until", None)
    windows = _read_windows(view, device_id, start, end)
    for date in _store_days(view, device_id, start, end):
        low, high = daystore.bounds(date)
        selected = [(max(a, low), min(b, high)) for a, b in windows if a < high and b > low]
        if selected:
            day = view._days.day(device_id, date, until=until)
            if day is not None:
                yield day, selected


def bounds(view, device_id, start=-math.inf, end=math.inf):
    """Outer observed endpoints from timestamp columns, without reconstructing vectors."""
    if math.isnan(start) or math.isnan(end) or end < start:
        raise ValueError("Invalid physical history window")
    start = max(start, getattr(view, "_physical_history_start", -math.inf))
    until = getattr(view, "_history_until", None)
    end = min(
        end, getattr(view, "_attribution_until", math.inf), math.inf if until is None else until
    )
    dates = _store_days(view, device_id, start, end)
    windows = _read_windows(view, device_id, start, end)
    first = next(filter(None, (_observed(view, device_id, d, windows) for d in dates)), None)
    if first is None:
        return 0, 0
    last = next(filter(None, (_observed(view, device_id, d, windows) for d in reversed(dates))))
    return first[0][0], last[-1][1]


def _observed(view, device_id, date, windows):
    """The observed bounds of each window inside ``date`` (only the outermost days with an
    observation are read: the bounds of the days between cannot matter)."""
    low, high = daystore.bounds(date)
    selected = [(max(a, low), min(b, high)) for a, b in windows if a < high and b > low]
    if not selected:
        return []
    day = view._days.day(device_id, date, until=getattr(view, "_history_until", None))
    if day is None:
        return []
    return [found for a, b in selected if (found := _column_bounds(day, a, b)) is not None]


def _column_bounds(day, low, high):
    first = bisect_right(day.ends, low)
    after = bisect_left(day.times, high)
    while first < after and min(high, day.ends[first]) <= max(low, day.times[first]):
        first += 1
    while first < after and min(high, day.ends[after - 1]) <= max(low, day.times[after - 1]):
        after -= 1
    if first < after:
        return max(low, day.times[first]), min(high, day.ends[after - 1])
    return None


def _read_windows(view, device_id, start, end):
    selected = getattr(view, "_learning_windows", {}).get(device_id)
    if selected is None:
        return [(start, end)] if end > start else []
    start = max(start, getattr(view, "_physical_history_start", -math.inf))
    until = getattr(view, "_history_until", None)
    end = min(
        end, getattr(view, "_attribution_until", math.inf), math.inf if until is None else until
    )
    return [
        (max(a, start), min(b, end))
        for a, b in _merged_windows(selected)
        if min(b, end) > max(a, start)
    ]


def _merged_windows(windows):
    merged = []
    for low, high in sorted(windows):
        if not math.isfinite(low) or not math.isfinite(high) or high < low:
            raise ValueError("Learning windows must have finite ordered endpoints")
        if high == low:
            continue
        if merged and low <= merged[-1][1]:
            merged[-1] = merged[-1][0], max(high, merged[-1][1])
        else:
            merged.append((low, high))
    return merged


def day_spans(day, start=-math.inf, end=math.inf):
    for index in range(bisect_right(day.ends, start), len(day.times)):
        if day.times[index] >= end:
            return
        low, high = max(start, day.times[index]), min(end, day.ends[index])
        if high > low:
            yield Span(low, high, day.vector(index), "event")


def events(view, device_id, *, start=-math.inf, end=math.inf):
    """The recorded spans of ``device_id`` within ``[start, end)``: query windows clip spans
    and never discard the value held at their start."""
    if math.isnan(start) or math.isnan(end) or end < start:
        raise ValueError("Invalid physical history window")
    start = max(start, getattr(view, "_physical_history_start", -math.inf))
    end = min(end, getattr(view, "_attribution_until", math.inf))
    return _disjoint(_stored(view, device_id, start, end)) if end > start else iter(())


def _disjoint(items):
    end = -math.inf
    for span in items:
        if span.start < end:
            raise ValueError("Physical history contains overlapping coverage")
        end = span.end
        yield span


def coverage(items, *, gate=None):
    """Observed physical time; missing readings never imply stable activity."""
    result = []
    for span in items:
        valid = (
            any(value is not None for value in span.values)
            if gate is None
            else span.values[gate] is not None
        )
        if not valid:
            continue
        if result and span.start == result[-1][1]:
            result[-1] = (result[-1][0], span.end)
        else:
            result.append((span.start, span.end))
    return result

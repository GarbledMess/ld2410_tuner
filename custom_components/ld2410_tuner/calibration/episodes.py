"""Independent explicit-label episodes over observed midpoint sample cells."""

from dataclasses import dataclass
from math import isfinite

LABELS = frozenset(("present", "not_present"))


@dataclass(frozen=True)
class Episode:
    start: float
    end: float
    state: str
    owner: tuple[str, ...]


def _time(value):
    try:
        value = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("episode times must be finite") from error
    if not isfinite(value):
        raise ValueError("episode times must be finite")
    return value


def _observations(rows):
    states = {}
    for row in rows:
        timestamp = _time(row[0])
        state = row[2] if row[2] in LABELS else None
        if timestamp in states and states[timestamp] != state:
            state = None
        states[timestamp] = state
    return sorted(states.items())


def _excluded(exclusions):
    result = []
    for start, end in exclusions:
        start, end = _time(start), _time(end)
        if end < start:
            raise ValueError("exclusions must have ordered endpoints")
        if end > start:
            result.append((start, end))
    return sorted(result)


def _pieces(start, end, exclusions):
    for low, high in exclusions:
        if high <= start or low > end:
            continue
        if start < low:
            yield start, min(low, end)
        start = max(start, high)
        if start >= end:
            return
    yield start, end


def _cells(observations, gap):
    for index, (timestamp, state) in enumerate(observations):
        previous = observations[index - 1][0] if index else timestamp
        following = observations[index + 1][0] if index + 1 < len(observations) else timestamp
        start = (previous + timestamp) / 2 if timestamp - previous <= gap else timestamp
        end = (timestamp + following) / 2 if following - timestamp <= gap else timestamp
        yield start, end, state


def episodes(rows, *, owner, gap, exclusions=()):
    """Merge same-state cells; gaps, unknowns and exclusions break independence.

    Unordered observations are sorted, equal duplicate states collapse, conflicting
    duplicates become unknown. Session endpoints add no unobserved elapsed time.
    Singleton observations retain a zero-duration episode. Empty exclusions are ignored.
    Exclusions are half-open pairs of
    finite ordered endpoints. Invalid times or negative gaps raise ValueError.
    """
    gap = _time(gap)
    if gap < 0:
        raise ValueError("gap must be nonnegative")
    excluded = _excluded(exclusions)
    result = []
    for start, end, state in _cells(_observations(rows), gap):
        if state not in LABELS:
            continue
        for low, high in _pieces(start, end, excluded):
            _append(result, Episode(low, high, state, tuple(owner)), excluded)
    return tuple(result)


def _append(result, item, exclusions):
    blocked = any(low <= item.start < high for low, high in exclusions)
    if result and not blocked and result[-1].state == item.state and result[-1].end == item.start:
        previous = result[-1]
        result[-1] = Episode(previous.start, item.end, item.state, item.owner)
    else:
        result.append(item)


def _valid_cut(items, cutoff):
    if any(item.start < cutoff < item.end for item in items):
        return False
    earlier = {item.state for item in items if item.start < cutoff}
    later = {item.state for item in items if item.start >= cutoff}
    return LABELS <= earlier and LABELS <= later


def chronological_split(items):
    """Choose a whole-episode common cutoff nearest the 80% episode-index target.

    Both explicit states must occur on both sides. Overlapping owners cannot be
    split independently. Equal-distance valid boundaries choose the earliest.
    """
    items = tuple(items)
    for item in items:
        if _time(item.end) < _time(item.start):
            raise ValueError("episodes must have ordered endpoints")
    cuts = [cut for cut in sorted({item.start for item in items}) if _valid_cut(items, cut)]
    return min(
        cuts,
        key=lambda cut: (abs(sum(e.start < cut for e in items) - 0.8 * len(items)), cut),
        default=None,
    )

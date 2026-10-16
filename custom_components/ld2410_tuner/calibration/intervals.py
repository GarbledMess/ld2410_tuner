"""Integrate piecewise confidence while callers retain their boundary semantics."""

from bisect import bisect_right
from itertools import repeat
from operator import add, mul, sub


class WeightedTime:
    """A prefix integral over ordered boundary times and their following weights.

    Callers supply telemetry midpoint cells or exact label boundaries explicitly.
    Queries must stay within the observed domain; no unobserved coverage is added.
    """

    def __init__(self, times, weights):
        self.times = times
        self.weights = weights
        self.prefix = [0.0]
        for i in range(1, len(times)):
            self.prefix.append(self.prefix[-1] + (times[i] - times[i - 1]) * weights[i - 1])

    def at(self, timestamp):
        index = max(0, bisect_right(self.times, timestamp) - 1)
        return self.prefix[index] + (timestamp - self.times[index]) * self.weights[index]

    def between(self, start, end):
        return self.at(end) - self.at(start)


def across(masses, starts, ends):
    """The sum of ``between`` over the spans ``(start, end)`` for each of ``masses`` (which
    share their times): the same terms in the same order, with each end located once."""
    times = masses[0].times
    if any(mass.times is not times for mass in masses):
        raise ValueError("Masses must share their times")
    if len(starts) != len(ends):
        raise ValueError("Every span needs a start and an end")
    firsts, lasts = _cells(times, starts), _cells(times, ends)
    begins = list(map(sub, starts, map(times.__getitem__, firsts)))
    finishes = list(map(sub, ends, map(times.__getitem__, lasts)))
    sums = []
    for mass in masses:
        # Per span: prefix[last] + (end - times[last]) * weights[last]
        #         - (prefix[first] + (start - times[first]) * weights[first]), summed in order.
        prefix, weights = mass.prefix, mass.weights
        high = map(
            add, map(prefix.__getitem__, lasts), map(mul, finishes, map(weights.__getitem__, lasts))
        )
        low = map(
            add, map(prefix.__getitem__, firsts), map(mul, begins, map(weights.__getitem__, firsts))
        )
        sums.append(sum(map(sub, high, low)))
    return sums


def _cells(times, values):
    """The index of the cell holding each value (the first cell for one before it)."""
    found = list(map(bisect_right, repeat(times), values))
    if 0 in found:
        return [k - 1 if k else 0 for k in found]
    return list(map(sub, found, repeat(1)))

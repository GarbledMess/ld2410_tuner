"""Integrate piecewise confidence while callers retain their boundary semantics."""

from bisect import bisect_right


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
    firsts = [k - 1 if (k := bisect_right(times, x)) else 0 for x in starts]
    lasts = [k - 1 if (k := bisect_right(times, x)) else 0 for x in ends]
    sums = []
    for mass in masses:
        prefix, weights = mass.prefix, mass.weights
        sums.append(
            sum(
                [
                    prefix[last]
                    + (end - times[last]) * weights[last]
                    - (prefix[first] + (start - times[first]) * weights[first])
                    for start, end, first, last in zip(starts, ends, firsts, lasts, strict=True)
                ]
            )
        )
    return sums

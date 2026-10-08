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

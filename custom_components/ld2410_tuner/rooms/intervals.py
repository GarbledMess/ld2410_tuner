"""Small interval operations shared by the room replay and its diagnostics."""

from bisect import bisect_right

from ..calibration.timing import _merge


def union(intervals):
    return _merge(sorted((a, b) for a, b in intervals if b > a))


def intersect(left, right):
    result, i, j = [], 0, 0
    while i < len(left) and j < len(right):
        a, b = left[i], right[j]
        start, end = max(a[0], b[0]), min(a[1], b[1])
        if end > start:
            result.append((start, end))
        if a[1] < b[1]:
            i += 1
        else:
            j += 1
    return result


def seconds(intervals):
    return sum(b - a for a, b in intervals)


class Timeline:
    """Look up disjoint half-open spans without scanning history for every point."""

    def __init__(self, spans):
        self.spans = sorted(spans)
        self.starts = [span[0] for span in self.spans]

    def at(self, timestamp, default=None):
        index = bisect_right(self.starts, timestamp) - 1
        if index < 0 or timestamp >= self.spans[index][1]:
            return default
        return self.spans[index][2] if len(self.spans[index]) > 2 else True

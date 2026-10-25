"""One-pass histogram queries for a reference rebuild's known day windows.

Keep Summary's prefix and boundary arithmetic exactly, without constructing reusable
per-bin indexes. Other history consumers continue to use the indexed Summary.
"""

from ..calibration.constants import SAMPLE_SECONDS
from ..history import summaries
from . import references


class BatchSummary(summaries.Summary):
    def _gate(self, column):
        return summaries._runs(column)

    def histograms_many(self, windows):
        ranges = [self._range(start, end) for start, end in windows]
        valid = [
            i
            for i, ((start, end), (first, after)) in enumerate(zip(windows, ranges, strict=True))
            if end > start and first < after
        ]
        indices = sorted({index for i in valid for index in ranges[i]})
        result = [[[0.0] * 101 for _ in self.gates] for _ in windows]
        for gate, runs in enumerate(self.gates):
            snapshots = _snapshots(runs, self.prefix, indices)
            for i in valid:
                first, after = ranges[i]
                bins = [b - a for a, b in zip(snapshots[first], snapshots[after], strict=True)]
                low, high = self._clipped(first, after, *windows[i], bins=True)
                self._subtract(bins, self.day.columns[gate], first, low)
                self._subtract(bins, self.day.columns[gate], after - 1, high)
                result[i][gate] = bins
        return result


def _snapshots(runs, prefix, indices):
    starts, ends, energies = runs
    mass, snapshots, position = [0.0] * 101, {}, 0
    for index in indices:
        while position < len(ends) and ends[position] <= index:
            _increment(mass, energies[position], prefix[ends[position]] - prefix[starts[position]])
            position += 1
        found = mass.copy()
        if position < len(starts) and starts[position] < index:
            _increment(found, energies[position], prefix[index] - prefix[starts[position]])
        snapshots[index] = found
    return snapshots


def _increment(mass, energy, seconds):
    if energy != summaries.MISSING:
        mass[energy] += seconds


class RebuildFold(references.SpanFold):
    """Keep normal admission/continuity/trimming; encode only retained periods.

    Each source is folded in time order, so switching away from a period can never
    revisit it. Its unencoded histogram can wait until the final retained set is known.
    """

    def __init__(self, profile):
        super().__init__(profile)
        self.pending = {}

    def close(self):
        retained = {id(period) for period in self.profile["periods"]}
        self.pending = {key: value for key, value in self.pending.items() if key in retained}
        if self.period is not None and id(self.period) in retained:
            self.pending[id(self.period)] = self.histograms

    def finish(self):
        self.close()
        for period in self.profile["periods"]:
            bins = self.pending.get(id(period))
            if bins is not None:
                period["data"] = references.encode(
                    {
                        key: {
                            energy: seconds / SAMPLE_SECONDS for energy, seconds in values.items()
                        }
                        for key, values in bins.items()
                    }
                )
        self.pending.clear()

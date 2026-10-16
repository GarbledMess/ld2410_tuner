"""Learner-only recorded runs and explicit observation summaries."""

from bisect import bisect_right
from dataclasses import dataclass, field
from math import inf
from types import MappingProxyType

from ..history import guesses
from ..history.label_lookup import _history_label_reader
from .episodes import Episode


@dataclass(frozen=True)
class Overlap:
    episode: Episode
    basis: int
    predicted: str
    agreement: float
    observed: float


@dataclass(frozen=True)
class AutomaticEpisode:
    episode: Episode
    basis: int
    confidence: float
    dependencies: tuple[str, ...]


@dataclass(frozen=True)
class QualityContext:
    episodes: tuple
    automatic_episodes: tuple
    overlaps: tuple
    gate_presence: object
    gate_empty: object
    fingerprint: str
    raw_runs: tuple = ()
    start: float = -inf
    end: float = inf
    gate_occupied_histograms: object = field(default_factory=dict)
    gate_empty_histograms: object = field(default_factory=dict)

    def __post_init__(self):
        for name in ("gate_presence", "gate_empty"):
            object.__setattr__(
                self, name, MappingProxyType({k: tuple(v) for k, v in getattr(self, name).items()})
            )
        for name in ("gate_occupied_histograms", "gate_empty_histograms"):
            object.__setattr__(
                self,
                name,
                MappingProxyType(
                    {
                        key: tuple(tuple(hist) for hist in value)
                        for key, value in getattr(self, name).items()
                    }
                ),
            )

    def raw_reader(self):
        return _WindowGuesses(self.raw_runs, self.start, self.end)

    def basis_at(self, timestamp):
        if not self.start <= timestamp < self.end:
            return guesses.UNKNOWN
        index = bisect_right(self.raw_runs, (timestamp, inf)) - 1
        if index >= 0 and timestamp <= self.raw_runs[index][1]:
            return self.raw_runs[index][4]
        return guesses.UNKNOWN


def _intersections(cells, runs):
    index = 0
    for low, high, manual in cells:
        while index < len(runs) and runs[index][1] <= low:
            index += 1
        current = index
        while current < len(runs) and runs[current][0] < high:
            run = runs[current]
            a, b = max(low, run[0]), min(high, run[1])
            if b > a:
                yield a, b, manual, run
            current += 1


def collect(view, device_id, keys, *, start=None, end=None, human_reader=None, owner=None):
    """Recollect the half-open training window before forming any aggregates."""
    start = -inf if start is None else start
    end = inf if end is None else end
    from . import event_quality

    return event_quality.collect(view, device_id, keys, start, end, human_reader, owner)


class _WindowGuesses(guesses.Guesses):
    """Recorded sample endpoints remain inclusive except the hidden training cutoff."""

    __slots__ = ("start", "end")

    def __init__(self, runs, start, end):
        super().__init__(runs)
        self.start, self.end = start, end

    def of_sample(self, timestamp):
        return super().of_sample(timestamp) if self.start <= timestamp < self.end else None

    def sample_guess(self, timestamp):
        return super().sample_guess(timestamp) if self.start <= timestamp < self.end else None


def _independent_reader(device, independent):
    """Only caller-proven independent labels supplement an absent own label."""
    own = _history_label_reader(device)
    if independent is None:
        return own

    def read(timestamp):
        label = own(timestamp)
        return independent(timestamp) if label is None else label

    read.edges = sorted(set(own.edges) | set(independent.edges))
    return read

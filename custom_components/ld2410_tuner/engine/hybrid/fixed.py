"""Fixed-pattern scoring at one common fidelity; never searches or refits.

Every measurement a facade report needs (sources, recent windows, held-out
episodes, members and Seen-by stretches) is a truth variant of a problem zone,
scored by the shared evaluator over the same replay records. Raw detection is
the same evaluator on the same rows with the radars' timing removed.
"""

from bisect import bisect_right
from dataclasses import replace

from ...calibration.episodes import Episode
from ...calibration.event_replay import _timing
from .evaluate import Evaluators, plan_scores
from .evidence import _labels
from .types import TruthSpan, ZoneSpec

SEPARATOR = "\x00"
STATES = ("present", "not_present")


def tag(group_id, *parts):
    """A unique evaluator zone id for one truth variant of ``group_id``."""
    return SEPARATOR.join((group_id, *parts))


def variant(zone, name, keep, members=None):
    """``zone`` restricted to the resolved truth pieces that ``keep`` accepts."""
    truth = tuple(
        TruthSpan(lo, hi, s.state, s.source, s.confidence, s.episode_id, s.dependencies)
        for lo, hi, s in _labels(zone.truth)
        if keep(lo, hi, s)
    )
    return ZoneSpec(tag(zone.group_id, name), tuple(members or zone.members), truth)


def source(zone, name, members=None):
    want = "human" if name == "human" else "automatic"
    return variant(zone, name, lambda _a, _b, s: _source(s) == want, members)


def _source(span):
    return "human" if span.source == "human" else "automatic"


def since(zone, name, cutoffs):
    """Truth from each class's cutoff onward (pieces are clipped, never reassigned)."""
    pieces = []
    for lo, hi, s in _labels(zone.truth):
        cut = cutoffs.get(s.state)
        if cut is not None and hi > cut:
            pieces.append(replace(s, start=max(lo, cut), end=hi))
    return ZoneSpec(tag(zone.group_id, name), zone.members, tuple(pieces))


def within(zone, name, intervals, members, state="present"):
    """``state`` truth inside ``intervals`` (sorted, disjoint) for ``members``."""
    intervals = sorted(intervals)
    ends = [b for _, b in intervals]
    pieces = []
    for lo, hi, s in _labels(zone.truth):
        if s.state == state:
            pieces.extend(_clipped(s, lo, hi, intervals, bisect_right(ends, lo)))
    return ZoneSpec(tag(zone.group_id, name), tuple(members), tuple(pieces))


def _clipped(span, lo, hi, intervals, at):
    while at < len(intervals) and intervals[at][0] < hi:
        a, b = max(lo, intervals[at][0]), min(hi, intervals[at][1])
        if b > a:
            yield replace(span, start=a, end=b)
        at += 1


def label_cutoffs(zone, share=0.2):
    """Where the newest ``share`` of each class's labelled duration begins."""
    pieces = list(_labels(zone.truth))
    found = {}
    for state in STATES:
        remaining = share * sum(hi - lo for lo, hi, s in pieces if s.state == state)
        for lo, hi, _ in reversed([p for p in pieces if p[2].state == state]):
            if hi - lo >= remaining:
                found[state] = hi - remaining
                break
            remaining -= hi - lo
    return found


def episodes(zone, owner):
    """Explicit human episodes of a zone, one per original episode identity."""
    found = {}
    for lo, hi, s in _labels(zone.truth):
        if s.source == "human":
            start, end, _ = found.get(s.episode_id, (lo, hi, s.state))
            found[s.episode_id] = min(start, lo), max(end, hi), s.state
    return [Episode(lo, hi, state, (owner,)) for lo, hi, state in found.values()]


def missed_seconds(metrics):
    occupied, recall = metrics["occupied_seconds"], metrics["occupied_recall"]
    return metrics.get("missed_seconds", occupied * (1 - recall) if recall is not None else 0.0)


def untimed(problem):
    radars = {rid: replace(radar, timing={}) for rid, radar in problem.radars.items()}
    return replace(problem, radars=radars)


class FixedScores:
    """Exact or timed-cell scores of fixed patterns; one plan for one bundle."""

    def __init__(self, problem, pattern_count, directory=None):
        self.problem = problem
        self.plan = plan_scores(problem, max(1, pattern_count), comparison=True)
        self.mode = "cells" if self.plan.mode == "cells" else "exact"
        self._timed = Evaluators(problem, self.plan, directory=directory)
        self._raw = (
            Evaluators(untimed(problem), self.plan, directory=directory)
            if any(any(_timing(radar.timing)) for radar in problem.radars.values())
            else self._timed
        )

    def scores(self, patterns, zones, *, raw=False):
        """Per pattern ``{zone id: metrics}`` for every zone in ``zones``."""
        evaluators = self._raw if raw else self._timed
        found = evaluators.get(self.mode).scores(list(patterns), zones=tuple(zones))
        return [score["zones"] for score in found]

    def close(self):
        self._timed.close()
        if self._raw is not self._timed:
            self._raw.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

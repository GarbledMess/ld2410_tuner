"""Exact common-cost scoring of compared patterns from shared, spilled replay records.

Physical activity is label-independent, so it is replayed once per unique radar
vector (``replay``) and every zone, truth variant and later re-score reads the same
records. Scorers are shared by identical member streams: a radar shared by zones,
or a pattern that differs only on other radars, never repeats replay or scoring.
"""

import tempfile
from dataclasses import replace
from pathlib import Path

from ...history import pace
from .constants import WARMUP_SECONDS
from .domains import attenuated_threshold
from .evidence import _labels
from .physical_index import physical_rows
from .replay import replay, support_tables
from .scoring import SliceScorer, _union
from .types import TruthSpan

LABELLED = ("present", "not_present")


def regions(truth, warmup=WARMUP_SECONDS):
    """Merged labelled windows, each extended backward by the timing warm-up."""
    return tuple(_union((s.start - warmup, s.end) for s in truth if s.state in LABELLED))


def bounded(problem, intervals):
    """Clip regions to the problem's immutable physical bounds."""
    clipped = ((max(problem.start, a), min(problem.end, b)) for a, b in intervals)
    return tuple(_union((a, b) for a, b in clipped if b > a))


def validate(problem, pattern):
    for rid, radar in problem.radars.items():
        values = pattern.get(rid)
        if values is None or set(values) != set(radar.channels):
            raise ValueError("Pattern must give a threshold for every configured channel")
        for threshold in values.values():
            if type(threshold) is not int or not 0 <= threshold <= 100:
                raise ValueError("Integer threshold 0..100 required")


def quiet_pattern(pattern):
    """Attenuated occupied energies, as the equivalent raised thresholds."""
    return {rid: {c: attenuated_threshold(t) for c, t in v.items()} for rid, v in pattern.items()}


def quiet_zone(zone):
    """Resolved occupied evidence only, keeping each piece's original episode."""
    pieces = (
        TruthSpan(lo, hi, s.state, s.source, s.confidence, s.episode_id, s.dependencies)
        for lo, hi, s in _labels(zone.truth)
        if s.state == "present"
    )
    return replace(zone, truth=tuple(pieces))


class _Physical:
    """Usable clipped rows from a production window source or generic blocks."""

    def __init__(self, source, channels):
        self.source, self.channels = source, channels

    def window(self, lo, hi):
        window = getattr(self.source, "window", None)
        if window is not None:
            return window(lo, hi)
        return physical_rows(self.source.blocks(lo, hi), lo, hi, self.channels).window(lo, hi)


class Evaluator:
    """Replay records for one fidelity and one set of label regions.

    ``scores`` replays only vectors not yet recorded; ``zones`` may carry other
    truth (for example audited masks) over the same regions without re-replay.
    Close it (or use it as a context manager) to remove every temporary file.
    """

    mode = "exact"

    def __init__(self, problem, regions, *, directory=None):
        self.problem = problem
        self.regions = bounded(problem, regions)
        self._temp = tempfile.TemporaryDirectory(dir=directory, prefix="hybrid-exact-")
        self.directory = Path(self._temp.name)
        models = problem.policy.quality.get("models", {})
        self.models = {
            rid: support_tables(radar, models.get(rid)) for rid, radar in problem.radars.items()
        }
        self.runs, self.where = [], {}
        self.operations = 0

    def scores(self, patterns, *, quiet=False, zones=None):
        """Scores aligned to ``patterns``; with ``quiet`` each carries its attenuated score."""
        patterns = list(patterns)
        for pattern in patterns:
            validate(self.problem, pattern)
        if not patterns:
            return []
        zones = tuple(self.problem.zones if zones is None else zones)
        jobs = [(p, "base") for p in patterns]
        if quiet:
            jobs += [(quiet_pattern(p), "quiet") for p in patterns]
        self._ensure(jobs)
        results = _Scoring(self, zones).run(jobs)
        out = results[: len(patterns)]
        for i, extra in enumerate(results[len(patterns) :]):
            out[i] = {**out[i], "quiet": extra}
        return out

    def providers(self):
        """Fresh row windows for one chronological replay run."""
        return {
            rid: _Physical(self.problem.rows[rid], radar.channels)
            for rid, radar in self.problem.radars.items()
        }

    def key(self, pattern, rid):
        return tuple(pattern[rid][c] for c in sorted(self.problem.radars[rid].channels))

    def _ensure(self, jobs):
        wanted = {}
        for pattern, _ in jobs:
            for rid in self.problem.radars:
                key = self.key(pattern, rid)
                if (rid, key) not in self.where and key not in wanted.setdefault(rid, []):
                    wanted[rid].append(key)
        wanted = {rid: keys for rid, keys in wanted.items() if keys}
        if not wanted:
            return
        records = replay(
            self.problem, self.providers(), self.regions, wanted, self.models, self.directory
        )
        self.operations += records.operations
        self.runs.append(records)
        for stream in records.streams:
            self.where[stream] = records

    @property
    def spilled_bytes(self):
        return sum(r.spilled_bytes for r in self.runs)

    def close(self):
        for records in self.runs:
            records.close()
        self.runs, self.where = [], {}
        self._temp.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class _Scoring:
    """One pass over the records feeding every distinct (variant, zone, streams) scorer."""

    def __init__(self, evaluator, zones):
        self.evaluator, self.zones = evaluator, zones
        self.variants = {"base": zones, "quiet": tuple(quiet_zone(z) for z in zones)}

    def run(self, jobs):
        problem = self.evaluator.problem
        scorers, plan = {}, []
        for pattern, variant in jobs:
            keys = []
            for z, zone in enumerate(self.variants[variant]):
                members = tuple((m, self.evaluator.key(pattern, m)) for m in zone.members)
                key = variant, z, members
                if key not in scorers:
                    scorers[key] = SliceScorer(
                        zone, problem.policy, (problem.start, problem.end), self.evaluator.directory
                    )
                keys.append(key)
            plan.append(keys)
        try:
            self._feed(scorers)
            results = {key: scorer.result() for key, scorer in scorers.items()}
        finally:
            for scorer in scorers.values():
                scorer.close()
        return [self._combine(keys, results) for keys in plan]

    def _feed(self, scorers):
        runs = self.evaluator.runs
        for records in runs:
            records.rewind()
        for _ in range(runs[0].steps if runs else 0):
            pace.rest()
            lo, hi, coverage, outputs = self._step(runs)
            for (_, _, members), scorer in scorers.items():
                signals = [{m: outputs[m, key][i] for m, key in members} for i in range(3)]
                scorer.consume(lo, hi, {m: coverage[m] for m, _ in members}, *signals)

    @staticmethod
    def _step(runs):
        coverage, outputs = {}, {}
        for records in runs:
            lo, hi, cov, out = records.read()
            coverage.update(cov)
            outputs.update(out)
        return lo, hi, coverage, outputs

    def _combine(self, keys, results):
        zones = {self.zones[key[1]].group_id: results[key] for key in keys}
        complete = all(r["cost"] is not None for r in zones.values())
        combined = dict(next(iter(zones.values()))) if len(zones) == 1 else {}
        combined.update(
            zones=zones,
            cost=sum(r["cost"] for r in zones.values()) if complete and zones else None,
            partial_cost=sum(r["partial_cost"] for r in zones.values()),
            scoring_mode=self.evaluator.mode,
        )
        return combined


def exact_scores(problem, patterns, regions):
    """Exact physical-row scores of ``patterns`` over ``regions`` (one shared replay)."""
    with Evaluator(problem, regions) as evaluator:
        return evaluator.scores(patterns)


__all__ = ["Evaluator", "bounded", "exact_scores", "quiet_zone", "regions"]

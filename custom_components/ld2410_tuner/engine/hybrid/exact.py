"""Exact common-cost scoring of compared patterns from shared, spilled replay records.

Physical activity is label-independent, so it is replayed once per unique radar
vector (``replay``) and every zone, truth variant and later re-score reads the same
records. Scorers are shared by identical member streams: a radar shared by zones,
or a pattern that differs only on other radars, never repeats replay or scoring.
"""

import struct
import tempfile
from array import array
from dataclasses import replace
from pathlib import Path

from ...history import pace
from .constants import SPILL_BYTES, WARMUP_SECONDS
from .domains import attenuated_threshold
from .evidence import _labels
from .physical_index import physical_rows
from .replay import replay, support_tables
from .scoring import ScoreStore, SliceScorer, _union
from .search_scores import zone_policy
from .types import TruthSpan

LABELLED = ("present", "not_present")
_WINDOW = struct.Struct("<ddQ")
_ROW_SPOOL_BYTES = 8 * 1024**2


def regions(truth, warmup=WARMUP_SECONDS):
    """Merged labelled windows, each extended backward by the timing warm-up."""
    return tuple(_union((s.start - warmup, s.end) for s in truth if s.state in LABELLED))


def bounded(problem, intervals):
    """Clip replay to captured physical bounds, retaining available timing warm-up.

    Production row sources expose their captured start, which can precede the
    scored window. SliceScorer still uses problem.start, so warm-up establishes
    timing without adding observations or labels to the requested assessment.
    """
    start = min(
        [problem.start, *(getattr(s, "start", problem.start) for s in problem.rows.values())]
    )
    start = max(start, problem.start - WARMUP_SECONDS)
    clipped = ((max(start, a), min(problem.end, b)) for a, b in intervals)
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


class _RowFiles:
    """One evaluator-owned file lease, independent of its number of radar spools."""

    def __init__(self):
        self.path, self.stream = None, None

    def acquire(self, path):
        if self.stream is None or self.path != path:
            self.close()
            self.stream = path.open("r+b" if path.exists() else "w+b")
            self.path = path
        return self.stream

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.path, self.stream = None, None


class _RowSpool:
    """Bounded memory prefix then capped files, with one transient disk handle."""

    def __init__(self, directory, budget=0, files=None):
        self.directory = Path(tempfile.mkdtemp(dir=directory, prefix="rows-"))
        self.position, self.size, self.stream = 0, 0, None
        self.memory = bytearray(budget)
        self.files = files

    @property
    def disk_bytes(self):
        return max(0, self.size - len(self.memory))

    @property
    def closed(self):
        return self.stream is None

    def seek(self, position):
        self.close()
        self.position = position

    def tell(self):
        return self.position

    def _open(self):
        if self.stream is None:
            offset = self.position - len(self.memory)
            path = self.directory / str(offset // _ROW_SPOOL_BYTES)
            self.stream = (
                path.open("r+b" if path.exists() else "w+b")
                if self.files is None
                else self.files.acquire(path)
            )
            self.stream.seek(offset % _ROW_SPOOL_BYTES)
        return self.stream

    def _advance(self, count):
        self.position += count
        if not (self.position - len(self.memory)) % _ROW_SPOOL_BYTES:
            self.close()

    def _prefix(self, view, *, write):
        count = min(len(view), max(0, len(self.memory) - self.position))
        piece = memoryview(self.memory)[self.position : self.position + count]
        if write:
            piece[:] = view[:count]
        else:
            view[:count] = piece
        self.position += count
        return view[count:]

    def _part(self, size):
        return min(size, _ROW_SPOOL_BYTES - (self.position - len(self.memory)) % _ROW_SPOOL_BYTES)

    def write(self, data):
        view = self._prefix(memoryview(data), write=True)
        while view:
            count = self._part(len(view))
            self._open().write(view[:count])
            self._advance(count)
            view = view[count:]
        self.size = max(self.size, self.position)

    def readinto(self, data):
        view = self._prefix(memoryview(data), write=False)
        while view:
            count = self._part(len(view))
            if self._open().readinto(view[:count]) != count:
                raise EOFError("Truncated physical replay window")
            self._advance(count)
            view = view[count:]

    def read(self, count):
        data = bytearray(min(count, self.size - self.position))
        self.readinto(data)
        return bytes(data)

    def close(self):
        if self.stream is not None:
            if self.files is None:
                self.stream.close()
            self.stream = None


class _Physical:
    """Usable row windows, spooled once for subsequent threshold replay runs."""

    def __init__(self, source, channels, directory, budget, files=None):
        self.source, self.channels = source, tuple(sorted(channels))
        self.file, self.size = _RowSpool(directory, budget, files), 0

    def rewind(self):
        self.file.seek(0)

    def window(self, lo, hi):
        try:
            return self._window(lo, hi)
        finally:
            self.file.close()

    def _window(self, lo, hi):
        head = self.file.read(_WINDOW.size)
        if head:
            return self._read(head, lo, hi)
        rows = self._load(lo, hi)
        times, ends, columns = rows
        self.file.write(_WINDOW.pack(lo, hi, len(times)))
        self.file.write(memoryview(times).cast("B"))
        self.file.write(memoryview(ends).cast("B"))
        for channel in self.channels:
            self.file.write(columns[channel])
        self.size = self.file.disk_bytes
        return rows

    def _load(self, lo, hi):
        window = getattr(self.source, "window", None)
        if window is not None:
            return window(lo, hi)
        return physical_rows(self.source.blocks(lo, hi), lo, hi, self.channels).window(lo, hi)

    def _read(self, head, lo, hi):
        start, end, count = _WINDOW.unpack(head)
        if (start, end) != (lo, hi):
            raise ValueError("Physical replay windows changed")
        times, ends = array("d", [0]) * count, array("d", [0]) * count
        self.file.readinto(memoryview(times).cast("B"))
        self.file.readinto(memoryview(ends).cast("B"))
        return times, ends, {channel: self.file.read(count) for channel in self.channels}

    def close(self):
        self.file.close()


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
        self._providers = {}
        self._row_files = _RowFiles()
        self.operations = 0

    def scores(self, patterns, *, quiet=False, zones=None, attribution=True):
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
        results = _Scoring(self, zones, attribution=attribution).run(jobs)
        out = results[: len(patterns)]
        for i, extra in enumerate(results[len(patterns) :]):
            out[i] = {**out[i], "quiet": extra}
        return out

    def providers(self):
        """Rewind the same disk-backed windows for every chronological replay run."""
        if not self._providers:
            budget = SPILL_BYTES // max(1, len(self.problem.radars))
            self._providers = {
                rid: _Physical(
                    self.source(rid), radar.channels, self.directory, budget, self._row_files
                )
                for rid, radar in self.problem.radars.items()
            }
        for provider in self._providers.values():
            provider.rewind()
        return self._providers

    def source(self, rid):
        """Physical windows underlying this evaluator's reusable row spool."""
        return self.problem.rows[rid]

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
        return sum(r.spilled_bytes for r in self.runs) + sum(
            p.size for p in self._providers.values()
        )

    def close(self):
        for records in self.runs:
            records.close()
        for provider in self._providers.values():
            provider.close()
        self.runs, self.where = [], {}
        self._providers.clear()
        self._row_files.close()
        self._temp.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class _Scoring:
    """One pass over the records feeding every distinct (variant, zone, streams) scorer."""

    def __init__(self, evaluator, zones, *, attribution=True):
        self.evaluator, self.zones = evaluator, zones
        self.attribution = attribution
        self.variants = {"base": zones, "quiet": tuple(quiet_zone(z) for z in zones)}

    def run(self, jobs):
        problem = self.evaluator.problem
        scorers, plan = {}, []
        with ScoreStore(self.evaluator.directory) as store:
            for pattern, variant in jobs:
                keys = []
                for z, zone in enumerate(self.variants[variant]):
                    members = tuple((m, self.evaluator.key(pattern, m)) for m in zone.members)
                    key = variant, z, members
                    if key not in scorers:
                        window = problem.start, problem.end
                        policy = zone_policy(problem, zone)
                        scorers[key] = SliceScorer(
                            zone,
                            policy,
                            window,
                            self.evaluator.directory,
                            store=store,
                            attribution=self.attribution,
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
            cache = {}
            for (_, _, members), scorer in scorers.items():
                signals = [{m: outputs[m, key][i] for m, key in members} for i in range(3)]
                scorer.consume(lo, hi, {m: coverage[m] for m, _ in members}, *signals, cache=cache)

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

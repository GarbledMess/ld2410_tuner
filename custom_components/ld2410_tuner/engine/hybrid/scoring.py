"""Shared base cost over bounded chronological replay slices.

Truth confidence is already admitted/corrected by the evidence adapter. ``supported``
is positive-only activity after quality filtering; it never masks physical false
activity. Quality audit counts (human_episodes/automatic_episode_mass, by state)
optionally supply dependency-deduplicated independent support for both cost terms.
Quiet-presence regularization belongs to training selection, never this cost.
"""

import sqlite3
import tempfile
from bisect import bisect_left
from itertools import groupby
from pathlib import Path

from ...calibration.scoring import automatic_share, episode_share, score_from_cost
from ...history import pace
from .constants import MISSED_WEIGHT
from .evidence import _intersect, _labels

STATES = ("present", "not_present")


def _union(intervals):
    out = []
    for lo, hi in sorted(intervals):
        if hi <= lo:
            continue
        if out and lo <= out[-1][1]:
            out[-1] = out[-1][0], max(hi, out[-1][1])
        else:
            out.append((lo, hi))
    return out


def _seconds(intervals):
    return sum(hi - lo for lo, hi in intervals)


def _clip(intervals, lo, hi):
    return list(_intersect(intervals, [(lo, hi)])) if hi > lo else []


def _room(values, members, lo, hi):
    return _clip(_union(piece for member in members for piece in values.get(member, ())), lo, hi)


def _coverage(values, members, lo, hi):
    shared = [(lo, hi)] if hi > lo and members else []
    for member in members:
        shared = list(_intersect(shared, values.get(member, ())))
    return shared


def _exclusive(values, members):
    """One sweep over activity edges, rather than a union per other-member set."""
    events = _activity_events(values, members)
    result = {member: [] for member in members}
    active = set()
    previous = None
    for edge, changes in groupby(sorted(events), key=lambda event: event[0]):
        if len(active) == 1 and previous is not None:
            result[next(iter(active))].append((previous, edge))
        _change_active(active, changes)
        previous = edge
    return result


def _activity_events(values, members):
    events = []
    for member in members:
        for lo, hi in values.get(member, ()):
            events.extend(((lo, member, 1), (hi, member, -1)))
    return events


def _change_active(active, changes):
    for _, member, direction in changes:
        if direction > 0:
            active.add(member)
        else:
            active.discard(member)


def _rates(values):
    return (values[1] / values[0] if values[0] else 0, values[3] / values[2] if values[2] else 0)


def _class_cost(statistics, policy, state):
    human = statistics.get(state + "/human", [0, 0, 0, 0])
    auto = statistics.get(state + "/automatic", [0, 0, 0, 0])
    ht, he = _rates(human)
    at, ae = _rates(auto)
    if not human[0]:
        return at, ae
    weight = 0 if policy.reference else policy.automatic_weight
    support = policy.quality
    hs = support.get("human_episodes", {}).get(state, human[2])
    au = support.get("automatic_episode_mass", {}).get(state, auto[2])
    eshare = (
        episode_share(weight, hs, au) if support else automatic_share(weight, human[2], auto[2])
    )
    tshare = eshare if support else automatic_share(weight, human[0], auto[0])
    return ht + tshare * at, he + eshare * ae


def cost_from_statistics(statistics, policy):
    """Evaluate source statistics [weighted time, error, episode mass, failed mass].

    Automatic-only classes stay primary even at zero requested weight/reference.
    Quality-aware influence uses independent support for both terms. Without a
    quality audit, time influence uses the legacy duration cap.
    No minimum duration or episode-count admission guard is applied.
    """
    components = {}
    time = episode = 0.0
    for state, factor in (("present", MISSED_WEIGHT), ("not_present", 1)):
        ct, ce = _class_cost(statistics, policy, state)
        components[state] = {"time_fraction": ct, "episode_fraction": ce}
        time += factor * ct
        episode += factor * ce
    complete = all(
        sum(statistics.get(s + "/" + src, [0])[0] for src in ("human", "automatic")) > 0
        for s in STATES
    )
    cost = 100 * (time + episode)
    return {
        "cost": cost if complete else None,
        "partial_cost": cost,
        "score": score_from_cost(cost) if complete else None,
        "scorable_both_classes": complete,
        "time_term": time,
        "episode_term": episode,
        "class_components": components,
    }


class _LongestMiss:
    def __init__(self):
        self.end = None
        self.length = self.longest = 0.0

    def add(self, lo, hi):
        self.length = self.length + hi - lo if self.end == lo else hi - lo
        self.end = hi
        self.longest = max(self.longest, self.length)

    def observe(self, observed, active):
        for lo, hi in observed:
            at = lo
            for a, b in _clip(active, lo, hi):
                if a > at:
                    self.add(at, a)
                at = b
            if at < hi:
                self.add(at, hi)


class SliceScorer:
    """Consume disjoint chronological slices; result finalizes and closes storage.

    Inputs are per-member sorted, coalesced interval lists for this slice only.
    Warm-up slices maintain trigger continuity but add no labelled statistics.
    Parent totals live in a bounded-cache disk index, never all-episode arrays.
    Use as a context manager (or close in finally) for cancellation/errors.
    """

    def __init__(self, zone, policy, window, directory: Path):
        self.zone, self.policy, self.window = zone, policy, window
        self._labels = iter(_labels(zone.truth))
        self._labelled = 0.0
        self._label = self._next_label()
        self._end = self._active_end = None
        self._longest = _LongestMiss()
        self._stats = {}
        self._totals = dict.fromkeys(STATES, 0.0)
        self._seen = dict(self._totals)
        self._raw = dict(self._totals)
        self._exclusive = dict.fromkeys(zone.members, 0.0)
        self._covered = 0.0
        self._result = None
        self._directory = tempfile.TemporaryDirectory(dir=directory, prefix="hybrid-score-")
        self._db = sqlite3.connect(Path(self._directory.name) / "episodes.sqlite")
        self._db.execute("PRAGMA cache_size=-256")
        self._db.execute("PRAGMA journal_mode=OFF")
        self._db.execute("""CREATE TABLE episodes (
            state TEXT, source TEXT, parent TEXT, observed REAL, weighted REAL,
            seen REAL, triggers INTEGER, PRIMARY KEY(state,source,parent))""")

    def consume(self, lo, hi, coverage, raw, active, supported):
        if self._db is None:
            raise ValueError("Scorer is closed")
        if hi < lo or (self._end is not None and lo < self._end):
            raise ValueError("Scoring slices must be chronological and disjoint")
        if hi == lo:
            return
        pace.rest()
        physical = _room(active, self.zone.members, lo, hi)
        starts = [a for a, _ in physical if a != self._active_end]
        self._active_end = physical[-1][1] if physical else None
        self._end = hi
        a, b = max(lo, self.window[0]), min(hi, self.window[1])
        shared = _coverage(coverage, self.zone.members, a, b)
        self._covered += _seconds(shared)
        signals = (
            physical,
            _room(raw, self.zone.members, lo, hi),
            _room(supported, self.zone.members, lo, hi),
        )
        self._consume_labels(a, b, shared, signals, starts, _exclusive(active, self.zone.members))

    def _consume_labels(self, lo, hi, coverage, signals, starts, members):
        while self._label is not None and self._label[1] <= lo:
            self._label = self._next_label()
        while self._label is not None and self._label[0] < hi:
            a, b, span = self._label
            observed = _clip(coverage, max(a, lo), min(b, hi))
            self._observe(span, observed, signals, starts, members)
            if b > hi:
                break
            self._label = self._next_label()

    def _observe(self, span, observed, signals, starts, members):
        mass = _seconds(observed)
        if not mass:
            return
        physical, raw, supported = signals
        state = span.state
        source = "human" if span.source == "human" else "automatic"
        seen = _seconds(_intersect(observed, physical))
        positive = supported if source == "automatic" and state == "present" else physical
        scored = _seconds(_intersect(observed, positive))
        self._totals[state] += mass
        self._seen[state] += seen
        self._raw[state] += _seconds(_intersect(observed, raw))
        error = mass - scored if state == "present" else scored
        stats = self._stats.setdefault(state + "/" + source, [0.0] * 4)
        stats[0] += mass * span.confidence
        stats[1] += error * span.confidence
        triggers = sum(bisect_left(starts, b) - bisect_left(starts, a) for a, b in observed)
        self._episode(span, source, mass, scored, triggers)
        if state == "present":
            self._longest.observe(observed, physical)
            self._attribute(observed, members)

    def _episode(self, span, source, mass, seen, triggers):
        self._db.execute(
            """INSERT INTO episodes VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(state,source,parent) DO UPDATE SET
            observed=observed+excluded.observed, weighted=weighted+excluded.weighted,
            seen=seen+excluded.seen, triggers=triggers+excluded.triggers""",
            (span.state, source, span.episode_id, mass, mass * span.confidence, seen, triggers),
        )

    def _next_label(self):
        label = next(self._labels, None)
        if label is not None:
            lo, hi, _ = label
            self._labelled += max(0, min(hi, self.window[1]) - max(lo, self.window[0]))
        return label

    def _attribute(self, observed, exclusive):
        for member, pieces in exclusive.items():
            self._exclusive[member] += _seconds(_intersect(observed, pieces))

    def _settle(self):
        counts = {
            "occupied_episodes": 0,
            "empty_periods": 0,
            "mostly_missed_episodes": 0,
            "completely_missed_episodes": 0,
            "empty_periods_with_false_trigger": 0,
            "empty_periods_mostly_false_active": 0,
            "empty_periods_failed": 0,
            "false_triggers": 0,
        }
        rows = self._db.execute(
            "SELECT state,source,observed,weighted,seen,triggers FROM episodes ORDER BY rowid"
        )
        for state, source, mass, weighted, seen, triggers in rows:
            pace.rest()
            bad = seen < 0.5 * mass if state == "present" else triggers > 0 or seen > 0.5 * mass
            stats = self._stats[state + "/" + source]
            confidence = weighted / mass
            stats[2] += confidence
            stats[3] += confidence * bad
            self._count(counts, state, mass, seen, triggers, bad)
        return counts

    @staticmethod
    def _count(counts, state, mass, seen, triggers, bad):
        if state == "present":
            counts["occupied_episodes"] += 1
            counts["mostly_missed_episodes"] += bad
            counts["completely_missed_episodes"] += seen == 0
        else:
            counts["empty_periods"] += 1
            counts["empty_periods_failed"] += bad
            counts["empty_periods_with_false_trigger"] += triggers > 0
            counts["empty_periods_mostly_false_active"] += seen > 0.5 * mass
            counts["false_triggers"] += triggers

    def _measures(self, seen):
        occupied = self._totals["present"]
        return {
            "occupied_seconds": occupied,
            "empty_seconds": self._totals["not_present"],
            "occupied_recall": seen["present"] / occupied if occupied else None,
            "false_active_seconds": seen["not_present"],
        }

    def result(self):
        if self._result is not None:
            return self._result
        try:
            while self._next_label() is not None:
                pace.rest()
            counts = self._settle()
            self._result = {
                **cost_from_statistics(self._stats, self.policy),
                **counts,
                **self._measures(self._seen),
                "sufficient_statistics": self._stats,
                "longest_miss_seconds": self._longest.longest,
                "uncovered_label_seconds": max(0.0, self._labelled - sum(self._totals.values())),
                "covered_seconds": self._covered,
                "raw": self._measures(self._raw),
                "exclusive_seconds": self._exclusive,
            }
            return self._result
        finally:
            self.close()

    def close(self):
        if self._db is not None:
            self._db.close()
            self._db = None
            self._directory.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

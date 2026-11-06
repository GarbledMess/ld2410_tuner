"""Shared base cost over bounded chronological replay slices.

Truth confidence is already admitted/corrected by the evidence adapter. ``supported``
is positive-only activity after quality filtering; it never masks physical false
activity. Quality audit counts (human_episodes/automatic_episode_mass, by state)
optionally supply dependency-deduplicated independent support for both cost terms.
Quiet-presence regularization belongs to training selection, never this cost.
"""

import sqlite3
import tempfile
from bisect import bisect_left, bisect_right
from dataclasses import replace
from itertools import accumulate, groupby
from operator import itemgetter, sub
from pathlib import Path

from ...calibration.scoring import automatic_share, episode_share, score_from_cost
from ...history import pace
from .constants import MISSED_WEIGHT
from .evidence import _intersect, _labels
from .types import TruthSpan

STATES = ("present", "not_present")
GUARD_FIELDS = ("mostly_missed_episodes", "empty_periods_mostly_false_active")
OVERLAP_CACHE_BYTES = 8 * 1024**2

_EPISODE_BATCH = 512


class Disjoint(list):
    """Replay-owned sorted, disjoint, positive-duration intervals of one chunk."""


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
    if len(intervals) == 1:
        return intervals[0][1] - intervals[0][0]
    return sum(hi - lo for lo, hi in intervals)


def _clip(intervals, lo, hi):
    if hi <= lo:
        return []
    a = bisect_right(intervals, lo, key=itemgetter(1))
    b = bisect_left(intervals, hi, key=itemgetter(0))
    part = intervals[a:b]
    if part:
        part[0] = max(lo, part[0][0]), part[0][1]
        part[-1] = part[-1][0], min(hi, part[-1][1])
    return part


class _Overlap:
    """Sorted disjoint intervals with a prefix integral: O(log n) clip and overlap."""

    __slots__ = ("ends", "items", "mass", "starts")

    def __init__(self, intervals):
        self.items = intervals
        self.starts = [a for a, _ in intervals]
        self.ends = [b for _, b in intervals]
        self.mass = list(accumulate((b - a for a, b in intervals), initial=0.0))

    def _range(self, lo, hi):
        return bisect_right(self.ends, lo), bisect_left(self.starts, hi)

    def clip(self, lo, hi):
        i, j = self._range(lo, hi)
        if j <= i or hi <= lo:
            return []
        part = self.items[i:j]
        part[0] = max(lo, part[0][0]), part[0][1]
        part[-1] = part[-1][0], min(hi, part[-1][1])
        return part

    def seconds(self, lo, hi):
        i, j = self._range(lo, hi)
        if j <= i or hi <= lo:
            return 0.0
        total = self.mass[j] - self.mass[i]
        return total - max(0.0, lo - self.starts[i]) - max(0.0, self.ends[j - 1] - hi)

    def within(self, observed):
        if len(observed) == 1:
            return self.seconds(*observed[0])
        return sum(self.seconds(a, b) for a, b in observed)


def _room(values, members, lo, hi):
    if len(members) == 1 and isinstance(values.get(members[0]), Disjoint):
        return _clip(values[members[0]], lo, hi)
    return _clip(_union(piece for member in members for piece in values.get(member, ())), lo, hi)


def _signal_overlaps(values, members, lo, hi, cache=None):
    """Identical member streams share their normalized activity and prefix integral."""
    cache = {} if cache is None else cache
    result = []
    for streams in values:
        pieces = tuple(streams.get(m, ()) for m in members)
        key = lo, hi, tuple(members), _stream_key(pieces)
        found = cache.get(key)
        if found is None:
            found = pieces, _Overlap(_room(streams, members, lo, hi))
            _cache_overlap(cache, key, found)
        result.append(found[1])
    return tuple(result)


def _stream_key(pieces):
    if sum(map(len, pieces)) <= 64:
        return tuple(tuple(piece[:]) for piece in pieces)
    return tuple(map(id, pieces))


def _cache_overlap(cache, key, found):
    size = len(found[1].items) * 160 + len(key[2]) * 128 + 2048
    _cache_value(cache, key, found, size)


def _cache_value(cache, key, found, size):
    used = cache.get("bytes", 0)
    if used + size <= OVERLAP_CACHE_BYTES:
        cache[key] = found
        cache["bytes"] = used + size


def _coverage(values, members, lo, hi):
    shared = [(lo, hi)] if hi > lo and members else []
    for member in members:
        pieces = values.get(member, ())
        if pieces != shared:
            shared = list(_intersect(shared, pieces))
    return shared


def _coverage_overlap(values, members, lo, hi, cache):
    cache = {} if cache is None else cache
    pieces = tuple(values.get(m, ()) for m in members)
    key = lo, hi, tuple(members), "coverage", _stream_key(pieces)
    found = cache.get(key)
    if found is None:
        found = pieces, _Overlap(_coverage(values, members, lo, hi))
        _cache_overlap(cache, key, found)
    return found[1]


def _exclusive(values, members):
    """One sweep over activity edges, rather than a union per other-member set."""
    if len(members) == 1:
        return {members[0]: list(values.get(members[0], ()))}
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


def _member_overlaps(values, members, lo, hi, cache, physical):
    if len(members) == 1:
        return {members[0]: physical}
    cache = {} if cache is None else cache
    pieces = tuple(values.get(m, ()) for m in members)
    key = lo, hi, tuple(members), "exclusive", _stream_key(pieces)
    found = cache.get(key)
    if found is None:
        indexes = {m: _Overlap(v) for m, v in _exclusive(values, members).items() if v}
        found = pieces, indexes
        size = sum(len(v.items) * 160 + 512 for v in indexes.values()) + len(members) * 128 + 2048
        _cache_value(cache, key, found, size)
    return found[1]


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


def _participates(statistics, policy, state, source):
    """Only evidence with nonzero episode influence participates in fit guards."""
    human = statistics.get(state + "/human", [0, 0, 0, 0])
    if source == "human" or not human[0]:
        return True
    auto = statistics.get(state + "/automatic", [0, 0, 0, 0])
    weight = 0 if policy.reference else policy.automatic_weight
    support = policy.quality
    hs = support.get("human_episodes", {}).get(state, human[2])
    au = support.get("automatic_episode_mass", {}).get(state, auto[2])
    share = episode_share(weight, hs, au) if support else automatic_share(weight, human[2], auto[2])
    return share > 0


def _guard_counts(statistics, policy, counts):
    """Observed integer counts of participating sources, separate from raw diagnostics."""
    included = [
        values
        for (state, source), values in counts.items()
        if _participates(statistics, policy, state, source)
    ]
    return {key: sum(values[key] for values in included) for key in GUARD_FIELDS}


def masked(zone, intervals):
    """Audited time becomes unobserved: an explicit Unknown outranks every label.

    Labels keep their episode identity around a masked stretch, and nothing is
    flipped to the other class or handed to a lower-precedence source.
    """
    intervals = _union(intervals)
    if not intervals:
        return zone
    extra = tuple(
        TruthSpan(a, b, "unknown", "human", 1.0, f"audit-mask-{i}", ())
        for i, (a, b) in enumerate(intervals)
    )
    return replace(zone, truth=zone.truth + extra)


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
            self._observe(lo, hi, active)

    def _observe(self, lo, hi, active):
        i, j = active._range(lo, hi)
        if j <= i:
            self.add(lo, hi)
            return
        if active.starts[i] > lo:
            self.add(lo, active.starts[i])
        gaps = map(sub, active.starts[i + 1 : j], active.ends[i : j - 1])
        self.longest = max(self.longest, max(gaps, default=0.0))
        if active.ends[j - 1] < hi:
            self.add(active.ends[j - 1], hi)
        else:
            self.end, self.length = None, 0.0


class ScoreStore:
    """One bounded-cache disk index of parent episode totals, shared by scorers.

    Each scorer owns an integer id, so a scoring pass opens one database instead
    of one per (pattern, zone); rows of a scorer keep their insertion order.
    """

    def __init__(self, directory: Path):
        self._directory = tempfile.TemporaryDirectory(dir=directory, prefix="hybrid-score-")
        self.db = sqlite3.connect(Path(self._directory.name) / "episodes.sqlite")
        self.db.execute("PRAGMA cache_size=-256")
        self.db.execute("PRAGMA journal_mode=OFF")
        self.db.execute("""CREATE TABLE episodes (
            scorer INTEGER, state TEXT, source TEXT, parent TEXT, observed REAL,
            weighted REAL, seen REAL, triggers INTEGER,
            PRIMARY KEY(scorer,state,source,parent))""")
        self._next = 0

    def allocate(self):
        self._next += 1
        return self._next

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
            self._directory.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class SliceScorer:
    """Consume disjoint chronological slices; result finalizes and closes storage.

    Inputs are per-member sorted, coalesced interval lists for this slice only.
    Warm-up slices maintain trigger continuity but add no labelled statistics.
    Parent totals live in a bounded-cache disk index, never all-episode arrays.
    Use as a context manager (or close in finally) for cancellation/errors.
    """

    def __init__(
        self,
        zone,
        policy,
        window,
        directory: Path,
        mask=(),
        store=None,
        *,
        attribution=True,
        labels=None,
    ):
        zone = masked(zone, mask)
        # Fixed report variants retain their base zone before the NUL tag.
        # Linked source support belongs to that zone, never the full set.
        support = policy.quality.get("zones", {}).get(zone.group_id.split("\x00", 1)[0])
        if support is not None:
            policy = replace(policy, quality={**policy.quality, **support})
        self.zone, self.policy, self.window = zone, policy, window
        self.attribution = attribution
        self._labels = iter(_labels(zone.truth) if labels is None else labels)
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
        self._owned = store is None
        self._store = ScoreStore(directory) if store is None else store
        self._db = self._store.db
        self._id = self._store.allocate()
        self._pending = []

    def consume(self, lo, hi, coverage, raw, active, supported, *, cache=None):
        if self._db is None:
            raise ValueError("Scorer is closed")
        if hi < lo or (self._end is not None and lo < self._end):
            raise ValueError("Scoring slices must be chronological and disjoint")
        if hi == lo:
            return
        pace.rest()
        signals = _signal_overlaps((active, raw, supported), self.zone.members, lo, hi, cache)
        physical = signals[0].items
        starts = [a for a, _ in physical if a != self._active_end]
        self._active_end = physical[-1][1] if physical else None
        self._end = hi
        a, b = max(lo, self.window[0]), min(hi, self.window[1])
        shared = _coverage_overlap(coverage, self.zone.members, a, b, cache)
        self._covered += _seconds(shared.items)
        members = (
            _member_overlaps(active, self.zone.members, lo, hi, cache, signals[0])
            if self.attribution
            else {}
        )
        self._consume_labels(a, b, shared, signals, starts, members)

    def _consume_labels(self, lo, hi, coverage, signals, starts, members):
        while self._label is not None and self._label[1] <= lo:
            self._label = self._next_label()
        while self._label is not None and self._label[0] < hi:
            a, b, span = self._label
            observed = coverage.clip(max(a, lo), min(b, hi))
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
        seen = physical.within(observed)
        positive = supported if source == "automatic" and state == "present" else physical
        scored = seen if positive is physical else positive.within(observed)
        self._totals[state] += mass
        self._seen[state] += seen
        self._raw[state] += seen if raw is physical else raw.within(observed)
        error = mass - scored if state == "present" else scored
        stats = self._stats.setdefault(state + "/" + source, [0.0] * 4)
        stats[0] += mass * span.confidence
        stats[1] += error * span.confidence
        triggers = sum(bisect_left(starts, b) - bisect_left(starts, a) for a, b in observed)
        self._episode(span, source, mass, scored, triggers)
        if state == "present":
            self._longest.observe(observed, physical)
            self._attribute(observed, members, physical, seen)

    def _episode(self, span, source, mass, seen, triggers):
        pending = self._pending
        if pending and pending[-1][1:4] == [span.state, source, span.episode_id]:
            row = pending[-1]
            row[4] += mass
            row[5] += mass * span.confidence
            row[6] += seen
            row[7] += triggers
            return
        row = [self._id, span.state, source, span.episode_id, mass]
        row.extend((mass * span.confidence, seen, triggers))
        pending.append(row)
        if len(pending) >= _EPISODE_BATCH:
            self._flush_episodes()

    def _flush_episodes(self):
        self._db.executemany(
            """INSERT INTO episodes VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(scorer,state,source,parent) DO UPDATE SET
            observed=observed+excluded.observed, weighted=weighted+excluded.weighted,
            seen=seen+excluded.seen, triggers=triggers+excluded.triggers""",
            self._pending,
        )
        self._pending.clear()

    def _next_label(self):
        label = next(self._labels, None)
        if label is not None:
            lo, hi, _ = label
            self._labelled += max(0, min(hi, self.window[1]) - max(lo, self.window[0]))
        return label

    def _attribute(self, observed, exclusive, physical, seen):
        for member, pieces in exclusive.items():
            self._exclusive[member] += seen if pieces is physical else pieces.within(observed)

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
        by_source = {}
        self._flush_episodes()
        rows = self._db.execute(
            """SELECT state,source,observed,weighted,seen,triggers FROM episodes
            WHERE scorer=? ORDER BY rowid""",
            (self._id,),
        )
        for state, source, mass, weighted, seen, triggers in rows:
            pace.rest()
            bad = seen < 0.5 * mass if state == "present" else triggers > 0 or seen > 0.5 * mass
            stats = self._stats[state + "/" + source]
            confidence = weighted / mass
            stats[2] += confidence
            stats[3] += confidence * bad
            self._count(counts, state, mass, seen, triggers, bad)
            own = by_source.setdefault((state, source), dict.fromkeys(counts, 0))
            self._count(own, state, mass, seen, triggers, bad)
        counts["guard_counts"] = _guard_counts(self._stats, self.policy, by_source)
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
            "missed_seconds": occupied - seen["present"],
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
            self._db = None
            if self._owned:
                self._store.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

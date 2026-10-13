"""Bounded, age-weighted room references. Guesses never become independent truth."""

import base64
import json
import zlib
from collections import Counter
from functools import lru_cache

from ..const import HISTORY_KEYS
from .inference import MIN_REFERENCE

VERSION = 1
DAY = 86400
MAX_PERIODS = 24  # Per source and state, independent of recording retention.
PERIOD_SECONDS = 1800
SOURCE_WEIGHT = {"human": 1.0, "entity": 0.5, "radar": 0.1}
STATES = ("present", "not_present")
# Device key: when the radar's references went out of date (presence/rebuilds.py).
STALE = "reference_stale_since"


def empty():
    return {"version": VERSION, "periods": []}


@lru_cache(maxsize=256)
def decode(encoded):
    return json.loads(zlib.decompress(base64.b64decode(encoded)))


def encode(histograms):
    raw = json.dumps(histograms, separators=(",", ":")).encode()
    return base64.b64encode(zlib.compress(raw)).decode("ascii")


def observe(profile, values, state, source, confidence, timestamp):
    observe_many(profile, [(timestamp, values, state, source, confidence)])


def observe_many(profile, observations):
    """Batch historical corrections without decompressing/repacking every sample."""
    fold = Fold(profile)
    for observation in observations:
        fold.add(observation)
    fold.close()


class Fold:
    """Observations ``(timestamp, values, state, source, confidence)`` folded one at a time
    into a profile's periods; ``close`` stores the histograms of the period still open. A
    period is decoded once and encoded once, however many samples it takes."""

    def __init__(self, profile):
        self.profile, self.period, self.histograms = profile, None, {}
        # Only a profile that already keeps too many periods of one kind needs trimming
        # on every sample; otherwise only a new period can make it so.
        self.trim = self._overfull()

    def add(self, observation):
        timestamp, values, state, source, confidence = observation
        current = self._period(state, source, confidence, timestamp)
        if current is None:
            return
        if current is not self.period:
            self.close()
            self.period = current
            self.histograms = {key: dict(bins) for key, bins in decode(current["data"]).items()}
        _add_values(self.histograms, values)
        current["end"] = timestamp
        current["count"] += 1
        current["confidence"] += (confidence - current["confidence"]) / current["count"]

    def close(self):
        if self.period is not None:
            self.period["data"] = encode(self.histograms)

    def _period(self, state, source, confidence, timestamp):
        if state not in STATES or source not in SOURCE_WEIGHT or confidence <= 0:
            return None
        periods = self.profile["periods"]
        previous = next((p for p in reversed(periods) if p["source"] == source), None)
        if previous and timestamp <= previous["end"]:
            return None
        began = not _continues(previous, state, timestamp)
        if began:
            previous = {
                "source": source,
                "state": state,
                "start": timestamp,
                "end": timestamp,
                "confidence": 0.0,
                "count": 0,
                "data": encode({}),
            }
            periods.append(previous)
        if began or self.trim:
            matching = [p for p in periods if (p["source"], p["state"]) == (source, state)]
            if len(matching) > MAX_PERIODS:
                periods.remove(matching[0])
            self.trim = self.trim and self._overfull()
        return previous

    def _overfull(self):
        kinds = Counter((p["source"], p["state"]) for p in self.profile["periods"])
        return any(count > MAX_PERIODS for count in kinds.values())


def _continues(previous, state, timestamp):
    return bool(
        previous
        and previous["state"] == state
        and 0 < timestamp - previous["end"] <= 10
        and timestamp - previous["start"] < PERIOD_SECONDS
    )


KEYS = frozenset(HISTORY_KEYS)
NAMES = tuple(str(index) for index in range(101))  # Bin names of whole readings.


def _add_values(histograms, values):
    for key, value in values.items():
        if key not in KEYS:
            continue
        if type(value) is int:
            if not 0 <= value <= 100:
                continue
            index = NAMES[value]
        elif 0 <= value <= 100:  # Not NaN, not infinite.
            index = str(round(value))
        else:
            continue
        bins = histograms.setdefault(key, {})
        bins[index] = bins.get(index, 0) + 1


def freshness(age):
    # Recent room conditions dominate; a smaller slow component retains rare quiet presence.
    days = max(0, age) / DAY
    return 0.8 * 2 ** (-days / 7) + 0.2 * 2 ** (-days / 60)


def distributions(profile, now):
    signature = tuple(
        (p["source"], p["state"], p["end"], p["confidence"], p["data"])
        for p in profile.get("periods", [])
    )
    return _distributions(signature, int(now // 60))


@lru_cache(maxsize=64)
def _distributions(signature, minute):
    now = minute * 60
    pools, latest, available = {}, {}, {}
    for source in SOURCE_WEIGHT:
        pools[source], seen, counted = _pool(tuple(e for e in signature if e[0] == source), minute)
        latest.update(seen)  # Keyed by source: disjoint.
        for slot, total in counted.items():
            available[slot] = available.get(slot, 0) + total
    reference = {}
    for key in HISTORY_KEYS:
        combined = _combine_channel(pools, key, latest, available, now)
        if combined:
            reference[key] = combined
    periods = [
        dict(zip(("source", "state", "end", "confidence", "data"), e, strict=True))
        for e in signature
    ]
    return reference, _confidence_cap(periods, now)


def _pool(entries, minute):
    """One source's histograms ``(pool, latest, available)`` from its periods, oldest first.
    Only the newest period of a source grows (every sample), so the rest are folded once
    (``_folded``) and the newest is added to a copy of that: the same additions in the same
    order, hence identical sums."""
    if not entries:
        return {}, {}, {}
    pool, latest, available = _folded(entries[:-1], minute)
    pool = {
        key: {state: list(bins) for state, bins in states.items()} for key, states in pool.items()
    }
    latest, available = dict(latest), dict(available)
    _fold(pool, latest, available, entries[-1], minute * 60)
    return pool, latest, available


@lru_cache(maxsize=32)
def _folded(entries, minute):
    pool, latest, available = {}, {}, {}
    for entry in entries:
        _fold(pool, latest, available, entry, minute * 60)
    return pool, latest, available


def _fold(pool, latest, available, entry, now):
    """Add one period to a source's ``pool`` (``key -> state -> bins``), its ``latest`` end
    per (source, key, state) and, for independent sources, the ``available`` sample count."""
    source, state, end, confidence, data = entry
    # Long runs do not get thousands of independent votes.
    weight = freshness(now - end) * confidence
    for key, bins in decode(data).items():
        histogram = pool.setdefault(key, {}).setdefault(state, [0.0] * 101)
        total = sum(bins.values())
        scale = min(1.0, 120 / total) * weight
        for value, count in bins.items():
            histogram[int(value)] += count * scale
        identity = (source, key, state)
        latest[identity] = max(latest.get(identity, 0), end)
        if source != "radar":
            available[(key, state)] = available.get((key, state), 0) + total


def _confidence_cap(periods, now):
    caps = []
    for period in periods:
        if period["source"] == "radar":
            continue
        ceiling = 0.98 if period["source"] == "human" else 0.90
        reliability = min(ceiling, period["confidence"])
        caps.append(reliability * (0.7 + 0.3 * freshness(now - period["end"])))
    return max(caps, default=0.65)


def _combine_channel(pools, key, latest, available, now):
    combined = {}
    for state in STATES:
        if available.get((key, state), 0) < MIN_REFERENCE:
            continue  # Repeated guesses cannot create their own reference support.
        histogram = [0.0] * 101
        for source, pool in pools.items():
            values = pool.get(key, {}).get(state, [])
            mass = sum(values)
            if not mass:
                continue
            strength = SOURCE_WEIGHT[source] * freshness(now - latest[(source, key, state)])
            scale = min(120, mass) * strength / mass
            histogram = [a + b * scale for a, b in zip(histogram, values, strict=True)]
        # Inference's minimum is an evidence-availability check, not a probability calibration.
        mass = sum(histogram)
        if mass:
            combined[state] = [value * max(1, (MIN_REFERENCE + 1e-9) / mass) for value in histogram]
    return combined


def unfamiliar(profile, values):
    """Flag channels outside both independent reference distributions."""
    support = _support(
        tuple(p["data"] for p in profile.get("periods", []) if p["source"] != "radar")
    )
    return [
        key
        for key, value in values.items()
        if support.get(key) and min(abs(value - seen) for seen in support[key]) > 6
    ]


def _support(data):
    """Every value either independent source has seen, per channel. Only the newest period
    grows (every sample), so the older ones are gathered once (``_older_support``)."""
    if not data:
        return {}
    support = {key: set(seen) for key, seen in _older_support(data[:-1]).items()}
    _gather(support, data[-1])
    return support


@lru_cache(maxsize=16)
def _older_support(data):
    support = {}
    for encoded in data:
        _gather(support, encoded)
    return support


def _gather(support, encoded):
    for key, bins in decode(encoded).items():
        support.setdefault(key, set()).update(int(value) for value in bins)


def summary(profile, now):
    periods = profile.get("periods", [])
    latest = max(
        (p["end"] for p in periods if p["source"] != "radar" and not p.get("imported_legacy")),
        default=None,
    )
    return {
        "version": VERSION,
        "periods": {
            source: sum(p["source"] == source for p in periods) for source in SOURCE_WEIGHT
        },
        "last_independent_label": latest,
        "legacy_age_unknown": any(p.get("imported_legacy") for p in periods),
        "reference_strength": freshness(now - latest) if latest is not None else 0,
    }

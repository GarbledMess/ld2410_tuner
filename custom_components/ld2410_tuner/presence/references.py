"""Bounded, age-weighted room references. Guesses never become independent truth."""

import base64
import json
import zlib
from bisect import bisect_left
from collections import Counter
from functools import lru_cache
from math import ceil, floor, isfinite

from ..calibration.constants import SAMPLE_SECONDS
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
        self._adopt(current)
        _add_values(self.histograms, values)
        current["end"] = timestamp
        current["count"] += 1
        current["confidence"] += (confidence - current["confidence"]) / current["count"]

    def _adopt(self, current):
        if current is not self.period:
            self.close()
            self.period = current
            self.histograms = {key: dict(bins) for key, bins in decode(current["data"]).items()}

    @staticmethod
    def _confidence(current, confidence, count):
        for _ in range(count):
            # Once the mean exactly equals this batch's value, every remaining
            # recurrence adds zero. Skip those steps without changing float bits.
            if current["confidence"] == confidence:
                current["count"] += count
                return
            current["count"] += 1
            current["confidence"] += (confidence - current["confidence"]) / current["count"]
            count -= 1

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


# Freshness halves over days: periods are re-weighed every 15 minutes (about 0.1 % of a
# weight), not every minute, and each radar at its own moment, so the radars' references are
# never all re-folded on the same tick of the event loop.
FRESHNESS_STEP = 900


def distributions(profile, now):
    signature = _signature(profile)
    return _distributions(signature, _moment(signature, now))


def moment(profile, now):
    """The time ``profile``'s periods are weighed at for a reading at ``now``."""
    return _moment(_signature(profile), now)


def _signature(profile):
    return tuple(
        (p["source"], p["state"], p["end"], p["confidence"], p["data"])
        for p in profile.get("periods", [])
    )


def _moment(signature, now):
    phase = zlib.crc32(repr(signature[:1]).encode()) % FRESHNESS_STEP  # Per radar.
    return floor((now + phase) / FRESHNESS_STEP) * FRESHNESS_STEP - phase


@lru_cache(maxsize=64)
def _distributions(signature, moment):
    now = moment
    pools, latest, available = {}, {}, {}
    for source in SOURCE_WEIGHT:
        pools[source], seen, counted = _pool(tuple(e for e in signature if e[0] == source), moment)
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


def _pool(entries, moment):
    """One source's histograms ``(pool, latest, available)`` from its periods, oldest first.
    Only the newest period of a source grows (every sample), so the rest are folded once
    (``_folded``) and the newest is added to a copy of that: the same additions in the same
    order, hence identical sums."""
    if not entries:
        return {}, {}, {}
    pool, latest, available = _folded(entries[:-1], moment)
    pool = {
        key: {state: list(bins) for state, bins in states.items()} for key, states in pool.items()
    }
    latest, available = dict(latest), dict(available)
    _fold(pool, latest, available, entries[-1], moment)
    return pool, latest, available


@lru_cache(maxsize=32)
def _folded(entries, moment):
    pool, latest, available = {}, {}, {}
    for entry in entries:
        _fold(pool, latest, available, entry, moment)
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
    support = _sorted_support(
        tuple(p["data"] for p in profile.get("periods", []) if p["source"] != "radar")
    )
    return [
        key
        for key, value in values.items()
        if support.get(key) and _distance(support[key], value) > 6
    ]


@lru_cache(maxsize=64)
def _sorted_support(data):
    """``_support`` sorted, kept per profile: it changes when evidence is folded in, about once
    a minute, and every sample in between asks it again."""
    return {key: tuple(sorted(seen)) for key, seen in _support(data).items()}


def _distance(seen, value):
    """How far ``value`` is from the nearest of the sorted values ``seen``: its neighbours."""
    index = bisect_left(seen, value)
    return min(abs(value - seen[i]) for i in (index - 1, index) if 0 <= i < len(seen))


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


class SpanFold:
    """Native duration/6 evidence, with exact fixed-clock periods and source boundaries.

    Input spans must arrive in chronological order. Their boundaries are physical
    coverage, not synthetic samples. No value is imputed across missing readings.
    Keeping a fold open decodes/encodes each surviving period only once.
    """

    def __init__(self, profile):
        self.profile, self.period, self.histograms = profile, None, {}

    def add(self, start, end, values, state, source, confidence):
        if not isfinite(start) or not isfinite(end) or end < start:
            raise ValueError("Reference span endpoints must be finite and ordered")
        values = _span_bins(values)
        if not _span_admitted(values, state, source, confidence):
            return
        while start < end:
            high = min(end, (floor(start / PERIOD_SECONDS) + 1) * PERIOD_SECONDS)
            self._add(start, high, values, state, source, confidence)
            start = high

    def add_histograms(self, start, end, histograms, state, source, confidence):
        """Fold one observed, fixed-clock piece using per-gate duration bins."""
        if not isfinite(start) or not isfinite(end) or end < start:
            raise ValueError("Reference span endpoints must be finite and ordered")
        values = _histogram_bins(histograms)
        if end == start or not _span_admitted(values, state, source, confidence):
            return
        if end > (floor(start / PERIOD_SECONDS) + 1) * PERIOD_SECONDS:
            raise ValueError("Reference histogram crosses a fixed-clock period")
        if not self._open(start, end, state, source, confidence):
            return
        for key, additions in values.items():
            bins = self.histograms.setdefault(key, {})
            for energy, seconds in additions.items():
                bins[energy] = bins.get(energy, 0) + seconds

    def _add(self, start, end, values, state, source, confidence):
        if self._open(start, end, state, source, confidence):
            for key, energy in values.items():
                bins = self.histograms.setdefault(key, {})
                bins[energy] = bins.get(energy, 0) + (end - start)

    def _open(self, start, end, state, source, confidence):
        previous = next(
            (
                p
                for p in reversed(self.profile["periods"])
                if p.get("kind") == "duration" and p["source"] == source
            ),
            None,
        )
        if previous is not None and start < previous["end"]:
            return
        if not _span_continues(previous, start, state):
            previous = self._new(start, state, source)
        if previous is not self.period:
            self.close()
            self.period = previous
            self.histograms = {  # Stored in six-second units; accumulated in seconds.
                key: {energy: units * SAMPLE_SECONDS for energy, units in bins.items()}
                for key, bins in decode(previous["data"]).items()
            }
        seconds = end - start
        old_seconds = previous["observed_seconds"]
        previous["observed_seconds"] = end - previous["start"]
        previous["count"] = previous["observed_seconds"] / SAMPLE_SECONDS
        previous["confidence"] = _span_mean(
            previous["confidence"], confidence, old_seconds, seconds
        )
        previous["end"] = end
        return True

    def _new(self, start, state, source):
        period = {
            "source": source,
            "state": state,
            "start": start,
            "end": start,
            "confidence": 0.0,
            "count": 0.0,
            "data": encode({}),
            "kind": "duration",
            "observed_seconds": 0.0,
        }
        self.profile["periods"].append(period)
        same = [p for p in self.profile["periods"] if p["source"] == source and p["state"] == state]
        for old in same[:-MAX_PERIODS]:
            self.profile["periods"].remove(old)
        return period

    def close(self):
        if self.period is not None:
            self.period["data"] = encode(
                {
                    key: {energy: seconds / SAMPLE_SECONDS for energy, seconds in bins.items()}
                    for key, bins in self.histograms.items()
                }
            )


def _span_bins(values):
    return {
        key: str(ceil(value))
        for key, value in values.items()
        if key in KEYS and isinstance(value, (int, float)) and isfinite(value) and 0 <= value <= 100
    }


def _span_admitted(values, state, source, confidence):
    return bool(
        values
        and state in STATES
        and source in SOURCE_WEIGHT
        and isfinite(confidence)
        and 0 < confidence <= 1
    )


def _span_continues(previous, start, state):
    return bool(
        previous
        and previous["state"] == state
        and previous["end"] == start
        and floor(previous["start"] / PERIOD_SECONDS) == floor(start / PERIOD_SECONDS)
    )


def _span_mean(previous, confidence, old_seconds, seconds):
    if not old_seconds or previous == confidence:
        return confidence
    return (previous * old_seconds + confidence * seconds) / (old_seconds + seconds)


def _histogram_bins(histograms):
    return {
        key: {str(energy): seconds for energy, seconds in enumerate(bins) if seconds > 0}
        for key, bins in zip(HISTORY_KEYS, histograms, strict=True)
        if any(seconds > 0 for seconds in bins)
    }

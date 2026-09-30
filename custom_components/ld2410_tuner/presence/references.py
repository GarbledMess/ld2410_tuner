"""Bounded, age-weighted room references. Guesses never become independent truth."""

import base64
import json
import math
import zlib
from functools import lru_cache

from ..const import HISTORY_KEYS
from .inference import MIN_REFERENCE

VERSION = 1
DAY = 86400
MAX_PERIODS = 24  # Per source and state, independent of recording retention.
PERIOD_SECONDS = 1800
SOURCE_WEIGHT = {"human": 1.0, "entity": 0.5, "radar": 0.1}
STATES = ("present", "not_present")


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
    period, histograms = None, {}
    for timestamp, values, state, source, confidence in observations:
        current = _period(profile, state, source, confidence, timestamp)
        if current is None:
            continue
        if current is not period:
            _store_period(period, histograms)
            period = current
            histograms = {key: dict(bins) for key, bins in decode(period["data"]).items()}
        _add_values(histograms, values)
        period["end"] = timestamp
        period["count"] += 1
        period["confidence"] += (confidence - period["confidence"]) / period["count"]
    _store_period(period, histograms)


def _store_period(period, histograms):
    if period is not None:
        period["data"] = encode(histograms)


def _period(profile, state, source, confidence, timestamp):
    if state not in STATES or source not in SOURCE_WEIGHT or confidence <= 0:
        return None
    periods = profile["periods"]
    previous = next((p for p in reversed(periods) if p["source"] == source), None)
    if previous and timestamp <= previous["end"]:
        return None
    if not _continues(previous, state, timestamp):
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
    matching = [p for p in periods if (p["source"], p["state"]) == (source, state)]
    if len(matching) > MAX_PERIODS:
        periods.remove(matching[0])
    return previous


def _continues(previous, state, timestamp):
    return bool(
        previous
        and previous["state"] == state
        and 0 < timestamp - previous["end"] <= 10
        and timestamp - previous["start"] < PERIOD_SECONDS
    )


def _add_values(histograms, values):
    for key, value in values.items():
        if key in HISTORY_KEYS and math.isfinite(value) and 0 <= value <= 100:
            bins = histograms.setdefault(key, {})
            index = str(round(value))
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
    periods = [
        dict(zip(("source", "state", "end", "confidence", "data"), entry, strict=True))
        for entry in signature
    ]
    pools = {source: {} for source in SOURCE_WEIGHT}
    latest = {}
    available = {}
    for period in periods:
        source = period["source"]
        _accumulate(pools[source], period, now)
        for key, bins in decode(period["data"]).items():
            identity = (source, key, period["state"])
            latest[identity] = max(latest.get(identity, 0), period["end"])
            if source != "radar":
                slot = (key, period["state"])
                available[slot] = available.get(slot, 0) + sum(bins.values())
    reference = {}
    for key in HISTORY_KEYS:
        combined = _combine_channel(pools, key, latest, available, now)
        if combined:
            reference[key] = combined
    return reference, _confidence_cap(periods, now)


def _confidence_cap(periods, now):
    caps = []
    for period in periods:
        if period["source"] == "radar":
            continue
        ceiling = 0.98 if period["source"] == "human" else 0.90
        reliability = min(ceiling, period["confidence"])
        caps.append(reliability * (0.7 + 0.3 * freshness(now - period["end"])))
    return max(caps, default=0.65)


def _accumulate(target, period, now):
    # Long runs do not get thousands of independent votes.
    weight = freshness(now - period["end"]) * period["confidence"]
    for key, bins in decode(period["data"]).items():
        histogram = target.setdefault(key, {}).setdefault(period["state"], [0.0] * 101)
        scale = min(1.0, 120 / sum(bins.values())) * weight
        for value, count in bins.items():
            histogram[int(value)] += count * scale


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
    support = {key: set() for key in values}
    for period in profile.get("periods", []):
        if period["source"] == "radar":
            continue
        for key, bins in decode(period["data"]).items():
            if key in support:
                support[key].update(int(value) for value in bins)
    return [
        key
        for key, value in values.items()
        if support[key] and min(abs(value - seen) for seen in support[key]) > 6
    ]


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

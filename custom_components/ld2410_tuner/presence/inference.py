"""Gate-energy autolabelling. Confidence is heuristic, never measured accuracy.

Human references are read-only. Inferred labels never enter those distributions.
All temporal state is bounded, JSON-serializable and independent of Learn/Apply.
"""

import math

MIN_REFERENCE = 20
MODEL = "temporal_evidence_v3"
MAX_GAP = 10.0
HUMAN_REFERENCE = "human-labelled distributions"
# Event-driven classification (see ``reclassify``). CHANGE: how far (energy units) any one
# channel must move from the readings last classified, and SUSTAIN: how long (seconds) it must
# stay moved. Chosen by replaying the recorded radars (tools/replay_autolabels.py): the 18
# channels of a quiet room already move 5-25 units from one sample to the next, so without
# SUSTAIN a move of 3 units reclassified about 90 % of the samples, and the larger moves that
# skip more of them cost the decided samples 1-2 points of accuracy. A move of 3 units that
# lasts 5 seconds skips 44-56 % of the classifications of the four labelled radars and moves
# no accuracy or coverage measure by more than 0.7 points. HEARTBEAT: the longest
# the last result is reused, in seconds (the references themselves are refreshed once a
# minute). SETTLED: a filter whose probability moved less than this in the last classification
# is at rest, so unchanged readings give the same answer.
CHANGE = 3.0
HEARTBEAT = 60.0
SETTLED = 0.05
SUSTAIN = 5.0  # Seconds a channel must stay moved before that counts as a change.


def quantile(histogram, fraction):
    target = max(1, math.ceil(sum(histogram) * fraction))
    count = 0
    for value, mass in enumerate(histogram):
        count += mass
        if count >= target:
            return value
    return 0


def _log_density(histogram, value):
    index = int(round(value))
    mass = sum(
        histogram[i] * (4 - abs(index - i)) / 4
        for i in range(max(0, index - 3), min(101, index + 4))
    )
    return math.log((mass + 0.5) / (sum(histogram) + 50.5))


def _combine_evidence(scores):
    positive = [score for score in scores if score > 0.05]
    negative = [score for score in scores if score < -0.05]
    if positive:
        # Distant quiet gates cannot veto a person seen by one gate, but
        # contradictory evidence must still lower weak, isolated positives.
        opposition = sum(negative) / len(negative) if negative else 0.0
        return max(positive) + 0.25 * max(-1.2, opposition)
    return sum(negative) / len(negative) if negative else 0.0


def _advance(previous, evidence, elapsed):
    # Integrate in six-second evidence units, including fractional ticks.
    # Consecutive two-second readings are correlated, not independent votes.
    remaining = elapsed
    while remaining > 1e-9:
        step = min(6.0, remaining)
        prior = 0.8 + (previous - 0.8) * (0.95 ** (step / 6))
        odds = math.log(prior / (1 - prior)) + evidence * step / 6
        previous = 1 / (1 + math.exp(-max(-20, min(20, odds))))
        remaining -= step
    return previous


def _start_filter(temporal, now):
    if temporal.get("model") != MODEL:
        temporal.clear()
        temporal["model"] = MODEL
    last = temporal.get("timestamp")
    elapsed = now - last if last is not None else 6.0
    previous = temporal.get("probability", 0.5)
    # A gap is time with no readings at all (``seen``). Readings that only reused the result
    # (``hold``) were unchanged and the filter at rest, so they leave it as it is; the new
    # reading stands for the time since the last of them.
    seen = temporal.get("seen", last if last is not None else now)
    if elapsed < 0 or now - seen > MAX_GAP:
        previous, elapsed = 0.5, 6.0
    elif last is not None:
        elapsed = now - seen
    temporal.update(timestamp=now, seen=now)
    return previous, elapsed


def _channel_observations(values, manual, background, temporal):
    channels, details = {}, []
    ranges = temporal.setdefault("ranges", {})
    for key, value in values.items():
        detail = _observe_channel(key, value, manual, background, ranges)
        if detail is not None:
            channels.setdefault(int(key[1]), []).append(detail.pop("raw_evidence"))
            details.append(detail)
    return channels, details, ranges


def _room_evidence(channels, calibration):
    gates = {gate: _combine_evidence(scores) for gate, scores in channels.items()}
    strongest = max(gates, key=gates.get) if gates else 0
    evidence = _combine_evidence(list(gates.values()))
    adjacent = max(
        (score for gate, score in gates.items() if abs(gate - strongest) == 1), default=0
    )
    if evidence > 0:
        evidence += min(0.7, max(0, adjacent) * 0.25)
    evidence += (
        float(calibration.get("absent_bias", 0)) - float(calibration.get("present_bias", 0)) * 0.25
    )
    return evidence, gates


def _classify_probability(
    probability,
    previous,
    elapsed,
    evidence,
    details,
    ranges,
    manual,
    values,
    complete,
    temporal,
    now,
):
    warmed_up = bool(details)
    all_ready = len(details) == len(values)
    # A completely steady startup has supplied no evidence that the room was
    # empty. Stay uncertain until there is a reference or observed variation.
    background_supported = any(
        sum(manual.get(d["key"], {}).get("not_present", [])) >= MIN_REFERENCE for d in details
    )
    background_supported |= any(hi - lo >= 3 for lo, hi in ranges.values())
    if evidence > 0.05:
        temporal["supported_at"] = now
    recent_support = previous >= 0.75 and now - temporal.get("supported_at", -math.inf) <= 12
    eligible = complete or any(
        d["source"] == HUMAN_REFERENCE and d["evidence"] > 1 for d in details
    )
    if not eligible or not warmed_up or (abs(evidence) < 0.05 and not recent_support):
        return "unknown", 0.0, 0.5 + (previous - 0.5) * (0.9 ** (elapsed / 6))
    if probability >= 0.75:
        return "present", probability, probability
    if probability <= 0.25 and complete and all_ready and background_supported:
        return "not_present", 1 - probability, probability
    return "unknown", 0.0, probability


def estimate_presence(values, manual, background, temporal, now, expected_keys, calibration=None):
    calibration = calibration or {}
    previous, elapsed = _start_filter(temporal, now)
    complete = bool(values) and set(expected_keys).issubset(values)
    channels, details, ranges = _channel_observations(values, manual, background, temporal)
    evidence, gates = _room_evidence(channels, calibration)
    probability = _advance(previous, evidence, elapsed)
    label, confidence, probability = _classify_probability(
        probability,
        previous,
        elapsed,
        evidence,
        details,
        ranges,
        manual,
        values,
        complete,
        temporal,
        now,
    )
    temporal["probability"] = max(0.01, min(0.99, probability))
    guided = any(d["source"] == HUMAN_REFERENCE for d in details)
    all_guided = bool(details) and all(d["source"] == HUMAN_REFERENCE for d in details)
    cap = _confidence_cap(guided, all_guided)
    if not complete or len(details) != len(values):
        cap = min(cap, 0.60)
    confidence = min(cap, confidence)
    details.sort(key=lambda row: row["evidence"], reverse=True)
    return {
        "label": label,
        "confidence": round(confidence, 3),
        "presence_probability": round(probability, 3),
        "score": round(evidence, 3),
        "active_gates": sum(v > 0 for v in gates.values()),
        "manual_guidance": guided,
        "top_gates": details[:4],
        "model": MODEL,
        "basis": "human-guided" if guided else "bootstrap",
    }


def confirm_estimate(result, runtime, now):
    """Confirm sustained evidence using elapsed time, never callback counts."""
    last = runtime.get("seen", runtime.get("timestamp"))
    if runtime.get("model") != result["model"] or (
        last is not None and not 0 <= now - last <= MAX_GAP
    ):
        runtime.clear()
    runtime.update(timestamp=now, seen=now, model=result["model"])
    label = result["label"]
    if label == "unknown":
        runtime.update(state="unknown", pending="unknown", pending_since=now)
        return "unknown"
    if label == "present" and runtime.get("state") != "present" and result["score"] <= 0:
        runtime.update(pending="unknown", pending_since=now)
        return "unknown"
    if runtime.get("pending") != label:
        runtime.update(pending=label, pending_since=now)
    needed = 4.0 if label == "present" else 8.0
    if now - runtime["pending_since"] < needed:
        return "unknown"
    runtime["state"] = label
    return label


def reclassify(cadence, values, now):
    """Whether the readings ``values`` at ``now`` need a new classification, or the last
    result can be reused (``hold``). ``cadence`` is the state ``classified`` left. Needed when
    nothing was classified yet, the last classification was not at rest (``at_rest``), the
    channels differ, one has stayed ``CHANGE`` or more away for ``SUSTAIN`` seconds (this keeps
    that time in ``cadence``), or ``HEARTBEAT`` seconds have passed."""
    anchor = cadence.get("anchor")
    if anchor is None or not cadence["rest"] or now - cadence["time"] >= HEARTBEAT:
        return True
    if anchor.keys() != values.keys():
        return True
    if not any(abs(value - values[key]) >= CHANGE for key, value in anchor.items()):
        cadence["moved"] = None
        return False
    # A lone spike is noise: the readings must stay moved for a few seconds.
    cadence["moved"] = now if cadence.get("moved") is None else cadence["moved"]
    return now - cadence["moved"] >= SUSTAIN


def at_rest(before, result, temporal, confirmed, now):
    """Whether reusing ``result`` (made from the filter at probability ``before``, confirmed as
    ``confirmed``) would answer as a new classification of unchanged readings would: nothing
    is waiting for confirmation, the filter has settled, and no timed hold-over is running."""
    steady = abs(result["presence_probability"] - before) < SETTLED
    supported = abs(result["score"]) < 0.05 and now - temporal.get("supported_at", -math.inf) <= 12
    return confirmed == result["label"] and steady and not supported


def classified(cadence, values, now, rest):
    """Remember a classification of ``values`` at ``now`` (``rest``: ``at_rest``)."""
    cadence.update(anchor=values, time=now, rest=rest, moved=None)


def hold(temporal, confirmation, now):
    """Reuse the last result at ``now``: the readings are still arriving, so the filter and
    the confirmation see no gap."""
    temporal["seen"] = confirmation["seen"] = now


def _observe_channel(key, value, manual, background, ranges):
    if not math.isfinite(value) or not 0 <= value <= 100:
        return None
    bounds = ranges.setdefault(key, [value, value])
    bounds[0], bounds[1] = min(bounds[0], value), max(bounds[1], value)
    p = manual.get(key, {}).get("present", [])
    n = manual.get(key, {}).get("not_present", [])
    baseline = n if sum(n) >= MIN_REFERENCE else background.get(key, [])
    if sum(baseline) < MIN_REFERENCE:
        return None
    q25, q75 = quantile(baseline, 0.25), quantile(baseline, 0.75)
    tail = quantile(baseline, 0.90)
    z = max(0.0, (value - tail) / max(2, q75 - q25))
    elevation = max(-0.6, min(3.5, (z - 1) * 1.2))
    guided = sum(p) >= MIN_REFERENCE and sum(n) >= MIN_REFERENCE
    if guided:
        likelihood = max(-3.5, min(3.5, _log_density(p, value) - _log_density(n, value)))
        # Typical empty-room energy cannot become positive evidence merely
        # because it also occurs in occupied examples. Conversely, a new
        # strong signal need not have appeared in the few occupied examples.
        evidence = (
            elevation + max(0, min(0.7, likelihood * 0.2))
            if elevation > 0
            else min(elevation, likelihood)
        )
    else:
        evidence = elevation
    return {
        "key": key,
        "energy": round(value, 1),
        "z": round(z, 2),
        "baseline_p25": q25,
        "baseline_p75": q75,
        "baseline_p90": tail,
        "evidence": round(evidence, 3),
        "raw_evidence": evidence,
        "source": HUMAN_REFERENCE if guided else "background estimate",
    }


def _confidence_cap(guided, all_guided):
    if all_guided:
        return 0.98
    return 0.80 if guided else 0.65

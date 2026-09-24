"""Temporal occupancy inference from learned energy distributions and gate geometry.

No pseudo-labels train the human reference distributions. The confidence score is
heuristic, not a calibrated probability of real-world occupancy.
"""
import math

MIN_REFERENCE = 20


def _quantile(histogram, fraction):
    target = max(1, math.ceil(sum(histogram) * fraction))
    count = 0
    for value, mass in enumerate(histogram):
        count += mass
        if count >= target:
            return value
    return 0


def _log_density(histogram, value):
    # A short triangular kernel tolerates integer quantization and small drift.
    index = int(round(value))
    mass = sum(histogram[i] * (4 - abs(index-i)) / 4 for i in range(max(0, index-3), min(101, index+4)))
    return math.log((mass + 0.5) / (sum(histogram) + 50.5))


def _combine_evidence(scores):
    positive = [score for score in scores if score > .05]
    if positive:
        return max(positive)
    negative = [score for score in scores if score < -.05]
    # A channel with identical occupied/empty distributions is uninformative;
    # it must not hide absence evidence from the channels that can distinguish.
    return sum(negative)/len(negative) if negative else 0.0


def estimate_presence(values, manual, background, temporal, now, expected_keys, calibration=None):
    calibration = calibration or {}
    complete = bool(values) and set(expected_keys).issubset(values)
    previous = temporal.get("probability", 0.5)
    if now - temporal.get("timestamp", 0) > 10:
        previous = 0.5
    temporal["timestamp"] = now
    channels, details = {}, []
    guided = False
    warmed_up = complete
    for key, value in values.items():
        p = manual.get(key, {}).get("present", [])
        n = manual.get(key, {}).get("not_present", [])
        baseline = n if sum(n) >= MIN_REFERENCE else background.get(key, [])
        ready = sum(baseline) >= MIN_REFERENCE
        warmed_up &= ready
        q25, q75 = _quantile(baseline, .25), _quantile(baseline, .75)
        z = max(0.0, (value - q75) / max(2, q75-q25))
        if sum(p) >= MIN_REFERENCE and sum(n) >= MIN_REFERENCE:
            evidence = max(-3.5, min(3.5, _log_density(p, value)-_log_density(n, value)))
            guided = True
            source = "human-labelled distributions"
        else:
            # Without labels, elevation above the background is weak evidence;
            # a steady signal could still be an occupied room during startup.
            evidence = max(-0.6, min(3.5, (z-1)*1.2)) if ready else 0.0
            source = "background estimate"
        gate = int(key[1])
        channels.setdefault(gate, []).append(evidence)
        details.append({"key": key, "energy": round(value, 1), "z": round(z, 2),
                        "baseline_p25": q25, "baseline_p75": q75,
                        "evidence": round(evidence, 3), "source": source})
    gates = {gate: _combine_evidence(scores) for gate, scores in channels.items()}
    strongest = max(gates, key=gates.get) if gates else 0
    evidence = _combine_evidence(list(gates.values()))
    # Move/still from one gate are correlated: they count once. Adjacent gates
    # add a small amount of support, never 18 independent votes for presence.
    adjacent = max((score for gate, score in gates.items() if abs(gate-strongest)==1), default=0)
    if evidence > 0:
        evidence += min(.7, max(0, adjacent)*.25)
    evidence += float(calibration.get("absent_bias", 0)) - float(calibration.get("present_bias", 0))*.25
    # A two-state Bayesian filter retains weak stationary evidence across time.
    # Exit is slower than entry; a single quiet frame does not erase occupancy.
    prior = previous*.99 + (1-previous)*.04
    log_odds = math.log(prior/(1-prior)) + evidence
    probability = 1/(1+math.exp(-max(-20, min(20, log_odds))))
    if not complete or not warmed_up or abs(evidence) < .05:
        label, confidence = "unknown", 0.0
        probability = previous + (.5-previous)*.1
    elif probability >= .75:
        label, confidence = "present", probability
    elif probability <= .25:
        label, confidence = "not_present", 1-probability
    else:
        label, confidence = "unknown", 0.0
    temporal["probability"] = max(.01, min(.99, probability))
    # Unknown/unguided channels limit confidence even when the temporal filter
    # has accumulated strong odds. This keeps unlabelled bootstrapping tentative.
    all_guided = bool(details) and all(d["source"] == "human-labelled distributions" for d in details)
    cap = .98 if all_guided else (.80 if guided else .65)
    confidence = min(cap, confidence)
    details.sort(key=lambda row: row["evidence"], reverse=True)
    return {
        "label": label, "confidence": round(confidence, 3),
        "presence_probability": round(probability, 3),
        "score": round(evidence, 3), "active_gates": sum(v > 0 for v in gates.values()),
        "manual_guidance": guided, "top_gates": details[:4],
        "model": "temporal_bayes_v1", "basis": "human-guided" if guided else "bootstrap",
    }

"""Labelled, device-wide threshold fitting; no Home Assistant dependencies.

The model approximates firmware detection as ANY enabled gate energy > threshold.
Validation is chronological within each class, not a claim of field accuracy.
"""
from __future__ import annotations

from math import floor

MIN_CLASS_SAMPLES = 50
MIN_RECALL = 0.999
MAX_FPR = 0.005
MAX_CLASS_SAMPLES = 5000
AUTO_WEIGHT = 0.20
AUTO_CLASS_CAP = 0.25  # At most 20% of combined class evidence when manual data exists.
MIN_AUTO_CONFIDENCE = 0.55
MAX_MISSED_RUN = 1
MAX_FALSE_BURSTS_PER_HOUR = 1.0
SAMPLE_SECONDS = 6.0


def metrics(rows, thresholds):
    counts = {"present": 0, "not_present": 0}
    hits = {"present": 0, "not_present": 0}
    for _timestamp, values, label in rows:
        counts[label] += 1
        hits[label] += any(values[key] > threshold for key, threshold in thresholds.items())
    return {
        **_temporal_metrics(rows, thresholds),
        "present_samples": counts["present"],
        "not_present_samples": counts["not_present"],
        "sensitivity": hits["present"] / counts["present"] if counts["present"] else 0.0,
        "false_positive_rate": hits["not_present"] / counts["not_present"] if counts["not_present"] else 0.0,
        "false_positives": hits["not_present"],
        "false_negatives": counts["present"] - hits["present"],
    }


def _temporal_metrics(rows, thresholds):
    episodes = missed_episodes = misses = longest_misses = bursts = 0
    previous = None
    episode_hit = False
    previous_false = False
    absent_seconds = 0.0
    for ts, values, label in sorted(rows, key=lambda row: row[0]):
        continuous = previous is not None and previous[1] == label and 0 <= ts-previous[0] <= SAMPLE_SECONDS*2
        hit = any(values[key] > threshold for key, threshold in thresholds.items())
        if not continuous:
            if previous and previous[1] == "present" and not episode_hit:
                missed_episodes += 1
            misses, episode_hit, previous_false = 0, False, False
            if label == "present":
                episodes += 1
        if label == "present":
            episode_hit |= hit
            misses = 0 if hit else misses+1
            longest_misses = max(longest_misses, misses)
        else:
            absent_seconds += min(SAMPLE_SECONDS, ts-previous[0]) if continuous else SAMPLE_SECONDS
            if hit and not previous_false:
                bursts += 1
            previous_false = hit
        previous = (ts, label)
    if previous and previous[1] == "present" and not episode_hit:
        missed_episodes += 1
    return {"presence_episodes": episodes, "missed_presence_episodes": missed_episodes,
            "longest_missed_run_samples": longest_misses, "false_trigger_bursts": bursts,
            "false_trigger_bursts_per_hour": bursts*3600/absent_seconds if absent_seconds else 0.0,
            "observed_absent_seconds": absent_seconds}


def _episode_masks(positives, negatives):
    """Keep short/quiet labelled episodes visible beside long active sessions."""
    ordered = sorted([(row[0], "present", i) for i, row in enumerate(positives)] +
                     [(row[0], "not_present", -1) for row in negatives])
    episodes, previous = [], None
    for timestamp, label, index in ordered:
        if label == "present":
            if previous is None or previous[1] != label or timestamp-previous[0] > SAMPLE_SECONDS*2:
                episodes.append(0)
            episodes[-1] |= 1 << index
        previous = (timestamp, label)
    return [(mask, mask.bit_count()) for mask in episodes]


def _masks(rows, key):
    bins = [0] * 101
    for i, (_ts, values, _label) in enumerate(rows):
        bins[values[key]] |= 1 << i
    masks = [0] * 101
    running = 0
    for threshold in range(100, -1, -1):
        masks[threshold] = running  # Strictly greater, as in the LD2410 protocol.
        running |= bins[threshold]
    return masks


def _weighted_masks(rows, manual_count):
    """Confidence buckets retain fractional weight; never round counts to integers."""
    total = sum(AUTO_WEIGHT * row[3] for row in rows)
    scale = min(1.0, manual_count * AUTO_CLASS_CAP / total) if manual_count and total else 1.0
    groups = {}
    for i, row in enumerate(rows):
        weight = AUTO_WEIGHT * round(row[3], 2) * scale
        groups[weight] = groups.get(weight, 0) | (1 << i)
    return groups, total * scale


def _weight(mask, groups):
    return sum(weight * (mask & bucket).bit_count() for weight, bucket in groups.items())


def _search(positives, negatives, auto_groups, keys):
    ap, an = auto_groups["present"], auto_groups["not_present"]
    pw, pmass = _weighted_masks(ap, len(positives))
    nw, nmass = _weighted_masks(an, len(negatives))
    allowed_fp = floor(len(negatives) * MAX_FPR)
    candidates = []
    for key in keys:
        pm, nm = _masks(positives, key), _masks(negatives, key)
        am = _masks([row[:3] for row in ap], key)
        bm = _masks([row[:3] for row in an], key)
        noise = sorted(row[1][key] for row in (negatives or an))
        noise_floor = noise[max(0, int(len(noise) * (1 - MAX_FPR)) - 1)]
        # The noise budget is the hard constraint. A two-point margin is a tie
        # preference, not a floor that would discard faint stationary presence.
        preferred = min(100, noise_floor + 2)
        candidates.extend((key, t, pm[t], nm[t], am[t], bm[t], preferred) for t in range(noise_floor, 100))
    episodes = _episode_masks(positives, negatives)
    thresholds = dict.fromkeys(keys, 100)
    detected = false = auto_detected = auto_false = 0
    while True:
        best, best_rank = None, None
        for key, threshold, pm, nm, am, bm, preferred in candidates:
            if threshold >= thresholds[key]:
                continue
            next_false = false | nm
            if next_false.bit_count() > allowed_fp:
                continue
            if not negatives and _weight(auto_false | bm, nw) > nmass * MAX_FPR + 1e-9:
                continue
            gain = (pm & ~detected).bit_count()
            auto_gain = _weight(am & ~auto_detected, pw) - _weight(bm & ~auto_false, nw)
            if not gain and auto_gain <= 1e-9:
                continue
            # Human presence coverage has first priority. Weighted guesses choose
            # among equal-coverage candidates and cover additional likely presence.
            covered = detected | pm
            newly_covered = sum(not (detected & mask) and bool(covered & mask) for mask, _ in episodes)
            worst_episode = min(((covered & mask).bit_count()/count for mask, count in episodes), default=0)
            rank = (newly_covered, worst_episode, gain, auto_gain, -next_false.bit_count(), -abs(threshold - preferred), -threshold)
            if best_rank is None or rank > best_rank:
                best, best_rank = (key, threshold, pm, nm, am, bm), rank
        if best is None:
            break
        key, threshold, pm, nm, am, bm = best
        thresholds[key] = threshold
        detected |= pm
        false |= nm
        auto_detected |= am
        auto_false |= bm
    return thresholds, {"present": pmass, "not_present": nmass}


def fit_thresholds(rows, keys, current=None, automatic=()):
    """Use confidence-weighted guesses for fitting; validate only human labels.

    Store guesses at observation time. Guesses at/after the first held-out label
    are deferred, so a classifier that has seen validation labels cannot leak
    them back into the fitting objective. Auto-only recommendations are provisional.
    """
    keys = list(dict.fromkeys(keys))
    groups = {"present": [], "not_present": []}
    for ts, values, label in sorted(rows, key=lambda row: row[0]):
        if label in groups and all(key in values for key in keys):
            groups[label].append((ts, values, label))
    groups = {label: group[-MAX_CLASS_SAMPLES:] for label, group in groups.items()}
    counts = {label: len(group) for label, group in groups.items()}
    ready = bool(keys) and min(counts.values()) >= MIN_CLASS_SAMPLES
    train, validation = [], []
    for group in groups.values():
        split = int(len(group) * 0.8) if ready else len(group)
        train.extend(group[:split])
        validation.extend(group[split:])
    cutoff = min((row[0] for row in validation), default=float("inf"))
    manual_times = {row[0] for group in groups.values() for row in group}
    auto_groups = {"present": [], "not_present": []}
    deferred = 0
    for ts, values, label, confidence in sorted(automatic, key=lambda row: row[0]):
        if label not in auto_groups or not MIN_AUTO_CONFIDENCE <= confidence <= 1 or not all(key in values for key in keys) or ts in manual_times:
            continue
        if ts >= cutoff:
            deferred += 1
            continue
        auto_groups[label].append((ts, values, label, confidence))
    auto_groups = {label: group[-MAX_CLASS_SAMPLES:] for label, group in auto_groups.items()}
    auto_counts = {label: len(group) for label, group in auto_groups.items()}
    evidence = {
        "samples": auto_counts, "deferred_samples": deferred,
        "base_weight": AUTO_WEIGHT, "class_weight_cap": AUTO_CLASS_CAP,
        "mean_confidence": {label: sum(row[3] for row in group) / len(group) if group else None for label, group in auto_groups.items()},
        "used": any(auto_counts.values()),
    }
    if not keys or any(counts[label] + auto_counts[label] < MIN_CLASS_SAMPLES for label in groups):
        return {
            "status": "insufficient", "proposals": {}, "counts": counts,
            "automatic_evidence": evidence,
            "warnings": [f"Need at least {MIN_CLASS_SAMPLES} complete observations of each state to propose thresholds. Human labels are required before Apply; automatic guesses already contribute to fitting."],
            "method": "joint_temporal_v2",
        }
    positives = [row for row in train if row[2] == "present"]
    negatives = [row for row in train if row[2] == "not_present"]
    thresholds, mass = _search(positives, negatives, auto_groups, keys)
    evidence["effective_weight"] = mass
    training = metrics(train, thresholds)
    held_out = metrics(validation, thresholds) if ready else None
    safe = ready and all(
        m["sensitivity"] >= MIN_RECALL and m["false_positive_rate"] <= MAX_FPR
        and m["missed_presence_episodes"] == 0 and m["longest_missed_run_samples"] <= MAX_MISSED_RUN
        and m["false_trigger_bursts_per_hour"] <= MAX_FALSE_BURSTS_PER_HOUR
        for m in (training, held_out)
    )
    status = ("ok" if safe else "unsafe") if ready else "provisional"
    warnings = []
    if not ready:
        warnings.append(f"Provisional recommendation using confidence-weighted guesses. Collect at least {MIN_CLASS_SAMPLES} human-labelled samples of each state to validate before Apply.")
    elif not safe:
        warnings.append("This candidate does not meet 99.9% presence recall, complete episode coverage, at most one consecutive missed sample, or the false-trigger limits on human-labelled training and validation. Application is blocked.")
    warnings.append("Confidence is a heuristic, not a calibrated probability. Validation uses human labels only; verify in separate real-room sessions, especially quiet sitting.")
    if deferred:
        warnings.append(f"{deferred} newer automatic samples are deferred until later human-labelled validation is available.")
    proposals = {}
    all_rows = train + validation
    for key, threshold in thresholds.items():
        m = metrics(all_rows, {key: threshold})
        noise = sorted(row[1][key] for row in (negatives or auto_groups["not_present"]))
        proposals[key] = {
            **m, "threshold": threshold, "status": status,
            "message": "" if safe else "Human-labelled validation required or target not met",
            "specificity": 1 - m["false_positive_rate"],
            "false_negative_rate": 1 - m["sensitivity"],
            "noise_ceiling": max(noise), "noise_floor_p99": noise[int((len(noise)-1)*0.99)],
            "noise_source": "manual" if negatives else "automatic",
            "safety_margin": 2, "auto_used": evidence["used"],
            "role": "suppressed" if threshold == 100 else "detection",
        }
    result = {
        "method": "joint_temporal_v2", "status": status,
        "proposals": proposals, "warnings": warnings, "training": training,
        "validation": held_out, "counts": counts, "keys": keys,
        "automatic_evidence": evidence,
        "targets": {"sensitivity": MIN_RECALL, "false_positive_rate": MAX_FPR, "missed_presence_episodes": 0, "longest_missed_run_samples": MAX_MISSED_RUN, "false_trigger_bursts_per_hour": MAX_FALSE_BURSTS_PER_HOUR},
    }
    if ready and current and all(key in current for key in keys):
        result["current_validation"] = metrics(validation, {key: current[key] for key in keys})
    return result

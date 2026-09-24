"""Labelled, device-wide threshold fitting; no Home Assistant dependencies.

The model approximates firmware detection as ANY enabled gate energy > threshold.
Validation is chronological within each class, not a claim of field accuracy.
"""
from __future__ import annotations

from math import floor, isfinite

MIN_CLASS_SAMPLES = 50
MIN_RECALL = 0.999
MAX_FPR = 0.005
MAX_CLASS_SAMPLES = 5000
METHOD = "human_priority_v4"
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
        hits[label] += any(values.get(key, -1) > threshold for key, threshold in thresholds.items())
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
        hit = any(values.get(key, -1) > threshold for key, threshold in thresholds.items())
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
                     [(row[0], "not_present", -1) for row in negatives], key=lambda row: row[0])
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
    for i, row in enumerate(rows):
        values = row[1]
        if key in values:
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


def _human_ranker(positives, negatives):
    """Rank target violations before refinements, for full and recent evidence.

    Bit links describe actual consecutive observations, not adjacent rows across
    gaps/label changes. A budgeted isolated miss is never a licence to lose an
    entire presence episode or a run of quiet presence.
    """
    windows = []
    starts = [(0, 0)]
    if min(len(positives), len(negatives)) >= MIN_CLASS_SAMPLES:
        starts.append((int(len(positives)*.8), int(len(negatives)*.8)))
    for pstart, nstart in starts:
        present, absent = positives[pstart:], negatives[nstart:]
        episodes = [mask << pstart for mask, _ in _episode_masks(present, absent)]
        empty_episodes = [mask << nstart for mask, _ in _episode_masks(absent, present)]
        presence_mask = ((1 << len(present))-1) << pstart
        absent_mask = ((1 << len(absent))-1) << nstart
        presence_links = presence_mask ^ sum(mask & -mask for mask in episodes)
        absent_links = absent_mask ^ sum(mask & -mask for mask in empty_episodes)
        seconds = _temporal_metrics(present + absent, {})["observed_absent_seconds"]
        windows.append((presence_mask, absent_mask, episodes, presence_links, absent_links,
                        floor(len(present)*(1-MIN_RECALL)+1e-9),
                        floor(len(absent)*MAX_FPR+1e-9),
                        floor(seconds*MAX_FALSE_BURSTS_PER_HOUR/3600+1e-9)))

    def rank(detected, false):
        violations = [0]*5
        refinements = None
        for pmask, nmask, episodes, plinks, nlinks, miss_budget, false_budget, burst_budget in windows:
            missed = pmask & ~detected
            false_here = nmask & false
            missed_count, false_count = missed.bit_count(), false_here.bit_count()
            bursts = (false_here & ~((false_here << 1) & nlinks)).bit_count()
            # Count runs exceeding the allowed length without scanning samples.
            too_long = missed
            for _ in range(MAX_MISSED_RUN):
                too_long = missed & (too_long << 1) & plinks
            failures = (sum(not (detected & mask) for mask in episodes),
                        max(0, missed_count-miss_budget), too_long.bit_count(),
                        max(0, false_count-false_budget), max(0, bursts-burst_budget))
            violations = [a+b for a, b in zip(violations, failures)]
            if refinements is None:
                refinements = (missed_count, false_count, bursts)
        return (*violations, *refinements)
    return rank


def _search(positives, negatives, auto_groups, keys, current=None):
    """Fit hardware thresholds to human targets, then lower-confidence evidence.

    Start near the background to retain useful redundant gates. If that search
    fails the targets, also start above observed empty-room values: correlated
    noisy gates can otherwise prevent any single-coordinate improvement.
    """
    ap, an = auto_groups["present"], auto_groups["not_present"]
    pw, pmass = _weighted_masks(ap, len(positives))
    nw, nmass = _weighted_masks(an, len(negatives))
    tables, preferred, quiet = {}, {}, {}
    human_rank = _human_ranker(positives, negatives)
    for key in keys:
        noise = sorted(row[1][key] for row in (negatives or an) if key in row[1])
        observed = any(key in row[1] for row in positives + negatives + ap + an)
        fallback = int((current or {}).get(key, 50))
        preferred[key] = min(100, noise[int((len(noise)-1)*.99)] + 2) if noise else fallback
        quiet[key] = max(noise) if noise else preferred[key]
        masks = [_masks(rows, key) for rows in (positives, negatives, ap, an)]
        candidates = range(101) if observed else [fallback]
        tables[key] = {t: tuple(mask[t] for mask in masks) for t in candidates}

    def rank(bits):
        detected, false, auto_detected, auto_false = bits
        auto_loss = (20 * (1 - _weight(auto_detected, pw)/pmass) if pmass else 0)
        auto_loss += _weight(auto_false, nw)/nmass if nmass else 0
        # Every human target and refinement outranks every automatic estimate.
        return (*human_rank(detected, false), round(auto_loss, 10))

    def optimize(seed):
        thresholds = dict(seed)
        selected = {key: tables[key][value] for key, value in thresholds.items()}
        for _step in range(len(keys)*4):
            combined = [0, 0, 0, 0]
            for bits in selected.values():
                for index, mask in enumerate(bits):
                    combined[index] |= mask
            distance = sum(abs(thresholds[key]-preferred[key]) for key in keys)
            best_rank = (*rank(combined), distance)
            best = None
            for key in keys:
                other = [0, 0, 0, 0]
                for other_key, bits in selected.items():
                    if other_key != key:
                        for index, mask in enumerate(bits):
                            other[index] |= mask
                for threshold, bits in tables[key].items():
                    candidate = tuple(a | b for a, b in zip(other, bits))
                    change_distance = distance-abs(thresholds[key]-preferred[key])+abs(threshold-preferred[key])
                    candidate_rank = (*rank(candidate), change_distance)
                    if candidate_rank < best_rank:
                        best_rank, best = candidate_rank, (key, threshold, bits)
            if best is None:
                break
            key, threshold, bits = best
            thresholds[key], selected[key] = threshold, bits
        return thresholds, best_rank

    def repair_pair(thresholds, score):
        # A noisy detector may be indispensable until a second gate is lowered.
        # Neither single change improves the score, so test the handover jointly.
        selected = {key: tables[key][thresholds[key]] for key in keys}
        distance = sum(abs(thresholds[key]-preferred[key]) for key in keys)
        options = {}
        for key in keys:
            unique = {}
            for threshold, bits in tables[key].items():
                previous = unique.get(bits)
                if previous is None or abs(threshold-preferred[key]) < abs(previous-preferred[key]):
                    unique[bits] = threshold
            options[key] = [(threshold, bits) for bits, threshold in unique.items()]
        best = None
        for noisy in keys:
            raises = [(t, bits) for t, bits in options[noisy]
                      if t > thresholds[noisy] and selected[noisy][1] & ~bits[1]]
            if not raises:
                continue
            for support in keys:
                if support == noisy:
                    continue
                lowers = [(t, bits) for t, bits in options[support]
                          if t < thresholds[support] and bits[0] & ~selected[support][0]]
                if not lowers:
                    continue
                other = [0]*4
                for key, bits in selected.items():
                    if key not in (noisy, support):
                        for index, mask in enumerate(bits):
                            other[index] |= mask
                base_distance = distance-abs(thresholds[noisy]-preferred[noisy])-abs(thresholds[support]-preferred[support])
                for raised, raised_bits in raises:
                    partial = tuple(a | b for a, b in zip(other, raised_bits))
                    lost = selected[noisy][0] & ~(partial[0] | selected[support][0])
                    if not lost:
                        continue
                    seen = set()
                    for lowered, lowered_bits in lowers:
                        if not (lowered_bits[0] & lost):
                            continue
                        candidate = tuple(a | b for a, b in zip(partial, lowered_bits))
                        if candidate in seen:
                            continue
                        seen.add(candidate)
                        candidate_score = (*rank(candidate), base_distance+abs(raised-preferred[noisy])+abs(lowered-preferred[support]))
                        if candidate_score < score:
                            score = candidate_score
                            best = {**thresholds, noisy: raised, support: lowered}
        return best

    thresholds, score = optimize(preferred)
    if any(score[:5]):
        # Any passing whole-device solution must satisfy each gate's individual
        # false-positive bounds. Start at the most sensitive such thresholds,
        # allowing several supporting gates to take over a noisy gate together.
        negative_windows = [((1 << len(negatives))-1, floor(len(negatives)*MAX_FPR+1e-9))]
        if min(len(positives), len(negatives)) >= MIN_CLASS_SAMPLES:
            start = int(len(negatives)*.8)
            negative_windows.append((((1 << len(negatives))-1) ^ ((1 << start)-1),
                                     floor((len(negatives)-start)*MAX_FPR+1e-9)))
        bounded = {key: next(t for t, bits in options.items()
                            if all((bits[1] & mask).bit_count() <= budget
                                   for mask, budget in negative_windows))
                   for key, options in tables.items()}
        for seed in (bounded, quiet):
            if seed == preferred:
                continue
            alternative, alternative_score = optimize(seed)
            if alternative_score < score:
                thresholds, score = alternative, alternative_score
    for _ in range(2):
        if not any(score[:5]):
            break
        repaired = repair_pair(thresholds, score)
        if repaired is None:
            break
        thresholds, score = optimize(repaired)
    return thresholds, {"present": pmass, "not_present": nmass}


def _human_failures(measured):
    failures = []
    if measured["present_samples"]:
        if measured["sensitivity"] < MIN_RECALL:
            failures.append(f"{measured['false_negatives']} human-labelled presence samples missed")
        if measured["missed_presence_episodes"]:
            failures.append(f"{measured['missed_presence_episodes']} human-labelled presence episodes missed")
        if measured["longest_missed_run_samples"] > MAX_MISSED_RUN:
            failures.append(f"{measured['longest_missed_run_samples']} consecutive human-labelled samples missed")
    if measured["not_present_samples"]:
        if measured["false_positive_rate"] > MAX_FPR:
            failures.append(f"{measured['false_positives']} false triggers in {measured['not_present_samples']} human-labelled empty-room samples")
        if measured["false_trigger_bursts_per_hour"] > MAX_FALSE_BURSTS_PER_HOUR:
            failures.append(f"{measured['false_trigger_bursts_per_hour']:.1f} false-trigger bursts per observed empty-room hour")
    return failures


def fit_thresholds(rows, keys, current=None, automatic=()):
    """Fit all usable evidence; human observations take absolute priority.

    Source proportions never block Apply. A chronological human backtest is
    reported separately, then the final recommendation is refit with ALL data.
    Its measurements are training evidence, not held-out accuracy guarantees.
    """
    keys = list(dict.fromkeys(keys))
    groups = {"present": [], "not_present": []}
    def clean(values):
        return {key: int(round(value)) for key, value in values.items()
                if key in keys and isinstance(value, (int, float)) and isfinite(value) and 0 <= value <= 100}
    manual_times = {row[0] for row in rows}
    for ts, values, label in sorted(rows, key=lambda row: row[0]):
        usable = clean(values)
        if label in groups and usable:
            groups[label].append((ts, usable, label))
    groups = {label: group[-MAX_CLASS_SAMPLES:] for label, group in groups.items()}
    counts = {label: len(group) for label, group in groups.items()}
    auto_groups = {"present": [], "not_present": []}
    for ts, values, label, confidence in sorted(automatic, key=lambda row: row[0]):
        usable = clean(values)
        if label in auto_groups and MIN_AUTO_CONFIDENCE <= confidence <= 1 and usable and ts not in manual_times:
            auto_groups[label].append((ts, usable, label, confidence))
    auto_groups = {label: group[-MAX_CLASS_SAMPLES:] for label, group in auto_groups.items()}
    auto_counts = {label: len(group) for label, group in auto_groups.items()}
    evidence = {
        "samples": auto_counts, "deferred_samples": 0,
        "base_weight": AUTO_WEIGHT, "class_weight_cap": AUTO_CLASS_CAP,
        "mean_confidence": {label: sum(row[3] for row in group) / len(group) if group else None for label, group in auto_groups.items()},
        "used": any(auto_counts.values()), "priority": "human_first",
    }
    if not keys or any(counts[label] + auto_counts[label] < MIN_CLASS_SAMPLES for label in groups):
        return {"status": "insufficient", "proposals": {}, "counts": counts,
                "automatic_evidence": evidence, "method": METHOD,
                "warnings": [f"Need {MIN_CLASS_SAMPLES} usable observations of each state, from human labels or confident estimates. Human: {counts}; automatic: {auto_counts}."]}

    # Backtest only earlier observations. Later estimates may have seen the
    # human holdout, so they must not enter this evaluation's fitting step.
    held_out = None
    if min(counts.values()) >= MIN_CLASS_SAMPLES:
        training_groups = {label: group[:int(len(group)*.8)] for label, group in groups.items()}
        validation = [row for label, group in groups.items() for row in group[len(training_groups[label]):]]
        cutoff = min(row[0] for row in validation)
        earlier_auto = {label: [row for row in group if row[0] < cutoff] for label, group in auto_groups.items()}
        backtest, _ = _search(training_groups["present"], training_groups["not_present"], earlier_auto, keys, current)
        held_out = metrics(validation, backtest)

    thresholds, mass = _search(groups["present"], groups["not_present"], auto_groups, keys, current)
    evidence["effective_weight"] = mass
    all_rows = groups["present"] + groups["not_present"]
    training = metrics(all_rows, thresholds)
    failures = _human_failures(training)
    recent = metrics(validation, thresholds) if held_out else None
    if recent:
        failures.extend("Recent human labels: " + failure for failure in _human_failures(recent))
    estimated = metrics([row[:3] for group in auto_groups.values() for row in group], thresholds)
    if not counts["present"] and estimated["present_samples"] and estimated["sensitivity"] == 0:
        failures.append("No estimated presence observations are detected by this candidate")
    if not counts["not_present"] and estimated["not_present_samples"] and estimated["false_positive_rate"] == 1:
        failures.append("This candidate triggers on every estimated empty-room observation")
    status = "unsafe" if failures else "ok"
    basis = "mixed" if all_rows and evidence["used"] else ("human" if all_rows else "automatic")
    warnings = list(failures)
    if basis != "human":
        warnings.append("Inferred observations contribute with lower confidence; their proportion does not block Apply. Human-labelled results always take priority.")
    if held_out and _human_failures(held_out):
        warnings.append("Earlier-data backtest missed its targets; the final recommendation was refit using all observations. Test it in new sessions.")
    warnings.append("These are observed training results, not proof of field accuracy. Automatic confidence is heuristic; verify quiet presence and empty-room behaviour.")
    proposals = {}
    for key, threshold in thresholds.items():
        measured = metrics(all_rows, {key: threshold})
        manual_noise = [row[1][key] for row in groups["not_present"] if key in row[1]]
        noise = sorted(manual_noise or [row[1][key] for row in auto_groups["not_present"] if key in row[1]])
        observed = any(key in row[1] for row in all_rows + auto_groups["present"] + auto_groups["not_present"])
        proposals[key] = {
            **measured, "threshold": threshold, "status": status,
            "message": "; ".join(failures), "evidence_basis": basis,
            "specificity": 1 - measured["false_positive_rate"],
            "false_negative_rate": 1 - measured["sensitivity"],
            "noise_ceiling": max(noise) if noise else None,
            "noise_floor_p99": noise[int((len(noise)-1)*.99)] if noise else None,
            "noise_source": "manual" if manual_noise else "automatic",
            "safety_margin": 2, "auto_used": evidence["used"],
            "role": "unchanged" if not observed else ("suppressed" if threshold == 100 else "detection"),
        }
    result = {
        "method": METHOD, "status": status, "evidence_basis": basis,
        "proposals": proposals, "warnings": warnings, "training": training,
        "validation": held_out, "validation_scope": "earlier_data_backtest",
        "recent_training": recent, "estimated_training": estimated,
        "counts": counts, "keys": keys, "automatic_evidence": evidence,
        "targets": {"sensitivity": MIN_RECALL, "false_positive_rate": MAX_FPR, "missed_presence_episodes": 0, "longest_missed_run_samples": MAX_MISSED_RUN, "false_trigger_bursts_per_hour": MAX_FALSE_BURSTS_PER_HOUR},
    }
    if held_out and current and all(key in current for key in keys):
        result["current_validation"] = metrics(validation, {key: current[key] for key in keys})
    return result

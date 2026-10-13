"""Reference: the room gate search as it was before it was made fast.

Every candidate of every gate is ranked in turn. The efficiency tests check the bounded search
against this on random rooms; nothing else uses it."""

from copy import deepcopy


def options(objective, device_id, key):
    radar = objective.radars[device_id]
    preferred = objective.preferred[device_id][key]
    current = objective.current[device_id][key]
    unique = {}
    for threshold, bits in enumerate(radar.tables[key]):
        previous = unique.get(bits)
        rank = (threshold != current, abs(threshold - preferred))
        if previous is None or rank < (previous != current, abs(previous - preferred)):
            unique[bits] = threshold
    return sorted({*unique.values(), preferred, current})


def sweep(objective, thresholds, candidates):
    best = objective.rank(thresholds)
    changed = False
    for (device_id, key), values in candidates.items():
        original, chosen = thresholds[device_id][key], thresholds[device_id][key]
        for value in values:
            thresholds[device_id][key] = value
            score = objective.rank(thresholds)
            if score < best:
                best, chosen = score, value
        thresholds[device_id][key] = chosen
        changed |= original != chosen
    return changed


def search(objective):
    candidates = {
        (device, key): options(objective, device, key)
        for device, gates in objective.current.items()
        for key in gates
    }
    best = deepcopy(objective.current)
    quiet = {device: dict.fromkeys(gates, 100) for device, gates in best.items()}
    for seed in (objective.current, objective.preferred, quiet):
        candidate = deepcopy(seed)
        for _ in range(4):
            if not sweep(objective, candidate, candidates):
                break
        if objective.rank(candidate) < objective.rank(best):
            best = candidate
    return best

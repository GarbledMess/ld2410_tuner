"""Joint gate search; weak radars need not detect what another member already covers."""

from copy import deepcopy

from . import assessment
from .objective import Objective

METHOD = "joint_room_v1"


def _options(objective, device_id, key):
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


def _sweep(objective, thresholds, options):
    best = objective.rank(thresholds)
    changed = False
    for (device_id, key), candidates in options.items():
        original, chosen = thresholds[device_id][key], thresholds[device_id][key]
        for value in candidates:
            thresholds[device_id][key] = value
            score = objective.rank(thresholds)
            if score < best:
                best, chosen = score, value
        thresholds[device_id][key] = chosen
        changed |= original != chosen
    return changed


def search(objective):
    options = {
        (device, key): _options(objective, device, key)
        for device, gates in objective.current.items()
        for key in gates
    }
    best = deepcopy(objective.current)
    quiet = {device: dict.fromkeys(gates, 100) for device, gates in best.items()}
    for seed in (objective.current, objective.preferred, quiet):
        candidate = deepcopy(seed)
        for _ in range(4):
            if not _sweep(objective, candidate, options):
                break
        if objective.rank(candidate) < objective.rank(best):
            best = candidate
    return best


def measure(members, thresholds, start, end):
    candidates = {key: {**item, "thresholds": thresholds[key]} for key, item in members.items()}
    return assessment.calculate(candidates, start, end)


def fit(members, start, end):
    objective = Objective(members, start, end)
    before = measure(members, objective.current, start, end)
    if before["room"]["score"] is None:
        raise ValueError(before["room"]["reason"])
    thresholds = search(objective)
    after = measure(members, thresholds, start, end)
    return {
        "method": METHOD,
        "thresholds": thresholds,
        "before": before,
        "after": after,
        "cost_before": objective.costs(objective.current)[0],
        "cost_after": objective.costs(thresholds)[0],
        "status": "uncertain" if after["room"]["target_met"] else "unsafe",
        "validation_scope": "training_replay_not_independent_validation",
    }

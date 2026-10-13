"""Joint gate search; weak radars need not detect what another member already covers."""

from copy import deepcopy

from ..calibration.settings import DEFAULT_AUTOMATIC_WEIGHT
from . import assessment
from .objective import LinkedObjective, Objective

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


SLACK = 1e-7  # Beyond the rounding of a rank entry and of the sums that make it.
TAIL = 3  # Entries of a rank that the thresholds alone decide (``Objective.tail``).


def _bisecting(count):
    """The indices ``0..count - 1``: the ends first, then the middle of what is left of them."""
    order, spans = ([0, count - 1] if count else []), [(0, count - 1)]
    for low, high in spans:
        if high - low > 1:
            mid = (low + high) // 2
            order.append(mid)
            spans += [(low, mid), (mid, high)]
    return list(dict.fromkeys(order))


def _probes(count, start):
    """The indices ``0..count - 1`` in the order a gate's candidates are tried: from ``start``
    outwards in strides that double, then the rest by bisecting. The best of a gate is mostly
    near where it is, and the candidates past where it gets worse are left out."""
    order, stride = [start], 1
    while stride < count:
        order += [start + stride, start - stride]
        stride *= 2
    order = [index for index in order if 0 <= index < count]
    return list(dict.fromkeys([*order, *_bisecting(count)]))


class _Gate:
    """The candidates of one gate, and the best of them found without ranking them all.

    A candidate is left out when it cannot beat the best so far. Its tail is known at once,
    so when the best so far costs nothing (no entry above zero, the least a rank can have)
    a candidate with a worse tail cannot win. And a gate hit more detects at least as much
    time and is false-active at least as much, so the ends of the candidates bound the
    cost of those between them (``bound``)."""

    def __init__(self, objective, thresholds, spot, values, best):
        device_id, key = spot
        self.objective, self.values, self.scores = objective, values, {}
        self.replayed = set()
        self.at = [{**thresholds, device_id: {**thresholds[device_id], key: v}} for v in values]
        self.tails = [objective.tail(item) for item in self.at]
        self.limit, self.holder = best, -1  # The best so far, and the candidate that has it.
        now = thresholds[device_id][key]
        self.start = min(range(len(values)), key=lambda index: abs(values[index] - now))

    def edge(self, index, step):
        """The nearest candidate from ``index`` in direction ``step`` that was replayed."""
        index += step
        while 0 <= index < len(self.values):
            if index in self.replayed:
                return index
            index += step
        return None

    def hopeless(self, index, low, high):
        """Whether the candidate cannot beat the best so far. Entries are compared in order,
        so while the best has none above zero (the least an entry can be) the candidate can
        only tie or lose there; at the first entry above zero the candidate loses when its
        bound is above it; and when every cost entry is zero, its tail decides."""
        bounds = None
        for position, entry in enumerate(self.limit[:-TAIL]):
            if entry:
                bounds = bounds or self.objective.bound(self.thresholds(low), self.thresholds(high))
                return bounds[position] > entry + SLACK
        return (self.tails[index], index) >= (self.limit[-TAIL:], self.holder)

    def thresholds(self, index):
        return None if index is None else self.at[index]

    def keep(self, index, score):
        self.scores[index] = score
        if (score, index) < (self.limit, self.holder):
            self.limit, self.holder = score, index

    def run(self):
        for index in _probes(len(self.values), self.start):
            low, high = self.edge(index, -1), self.edge(index, 1)
            if self.hopeless(index, low, high):
                continue
            if None not in (low, high) and self.objective.alike(self.at[low], self.at[high]):
                # Every candidate between two alike ones is alike: only its tail differs.
                self.keep(index, (*self.scores[low][:-TAIL], *self.tails[index]))
            else:
                self.keep(index, self.objective.rank(self.at[index]))
                self.replayed.add(index)
        return self.holder if self.holder >= 0 else None


def _sweep(objective, thresholds, options):
    best = objective.rank(thresholds)
    changed = False
    for (device_id, key), candidates in options.items():
        original = thresholds[device_id][key]
        gate = _Gate(objective, thresholds, (device_id, key), candidates, best)
        index = gate.run()
        if index is not None:
            best = gate.limit
            thresholds[device_id][key] = candidates[index]
            changed |= original != candidates[index]
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


def measure(members, thresholds, start, end, room=None):
    candidates = {key: {**item, "thresholds": thresholds[key]} for key, item in members.items()}
    return assessment.calculate(candidates, start, end, room)


def fit(members, start, end, room=None, automatic_weight=DEFAULT_AUTOMATIC_WEIGHT):
    objective = Objective(members, start, end, room, automatic_weight=automatic_weight)
    before = measure(members, objective.current, start, end, room)
    if before["room"]["score"] is None:
        raise ValueError(before["room"]["reason"])
    thresholds = search(objective)
    after = measure(members, thresholds, start, end, room)
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


def _current(members):
    return {key: dict(item["thresholds"]) for key, item in members.items()}


def _scorable(groups, start, end, rooms):
    """Before-reports of the groups that can be scored, and the reasons the rest cannot.
    Raises the first group's reason (as ``fit`` would) when no group can be scored."""
    before, skipped = {}, {}
    for gid in sorted(groups):
        report = measure(groups[gid], _current(groups[gid]), start, end, rooms.get(gid))
        if report["room"]["score"] is None:
            skipped[gid] = report["room"]["reason"]
        else:
            before[gid] = report
    if not before:
        raise ValueError(next(iter(skipped.values())))
    return before, skipped


def _top_level(groups, reports, radars):
    """One group: its own report, identical to ``fit``. Several: no room truth spans the
    set, so ``room`` is None and ``devices`` holds each radar's measure in every scored
    group, ``{radar: {group_id: device report}}``."""
    if len(groups) == 1:
        return next(iter(reports.values()))
    return {
        "room": None,
        "devices": {
            radar: {
                gid: item["devices"][radar]
                for gid, item in reports.items()
                if radar in item["devices"]
            }
            for radar in radars
        },
    }


def _group_results(groups, before, after, skipped, objective, thresholds):
    costs = {
        gid: (item.costs(part)[0], item.costs(thresholds)[0])
        for gid, item, part in objective.parts(objective.current)
    }
    return {
        gid: {"skipped": skipped[gid]}
        if gid in skipped
        else {
            "before": before[gid],
            "after": after[gid],
            "cost_before": costs[gid][0],
            "cost_after": costs[gid][1],
        }
        for gid in sorted(groups)
    }


def fit_set(groups, start, end, rooms=None, automatic_weight=DEFAULT_AUTOMATIC_WEIGHT):
    """Fit a learning set together: ``fit_set(groups, start, end, rooms=None, automatic_weight)``.

    ``groups`` is ``{group_id: members}``; ``rooms`` is ``{group_id: truth.view snapshot}``
    (missing: no room labels). Each group is scored on its own room truth; groups that
    cannot be scored are skipped (``groups[gid] == {"skipped": reason}``) and the set is
    fitted on the rest. A radar serving only skipped groups keeps its current threshold
    and is not searched. Raises only when no group can be scored.

    Returns ``fit``'s keys plus ``groups``. ``cost_before``/``cost_after`` are sums over the
    scored groups (what the search minimises); ``status`` is "uncertain" only when every
    scored group meets its target. A one-group set is identical to ``fit``. For several
    groups the top-level ``before``/``after`` have ``room`` None and per-radar measures
    only: callers must use ``groups`` for per-zone results."""
    if not groups:
        raise ValueError("No zones to learn")
    rooms = rooms or {}
    before, skipped = _scorable(groups, start, end, rooms)
    objective = LinkedObjective(
        {gid: groups[gid] for gid in before}, start, end, rooms, automatic_weight
    )
    thresholds = search(objective)
    for gid in skipped:
        for key, value in _current(groups[gid]).items():
            thresholds.setdefault(key, value)
    after = {gid: measure(groups[gid], thresholds, start, end, rooms.get(gid)) for gid in before}
    return {
        "method": METHOD,
        "thresholds": thresholds,
        "before": _top_level(groups, before, objective.current),
        "after": _top_level(groups, after, objective.current),
        "cost_before": objective.costs(objective.current)[0],
        "cost_after": objective.costs(thresholds)[0],
        "status": "uncertain"
        if all(item["room"]["target_met"] for item in after.values())
        else "unsafe",
        "validation_scope": "training_replay_not_independent_validation",
        "groups": _group_results(groups, before, after, skipped, objective, thresholds),
    }

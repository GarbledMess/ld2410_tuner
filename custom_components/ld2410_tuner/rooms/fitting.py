"""Joint room learning, run by the hybrid engine (``engine.hybrid.facade``).

``measure``, ``fit`` and ``fit_set`` keep their signatures and report shapes; weak radars
need not detect what another member already covers. ``search`` and its gate helpers are
the superseded row-objective search: no production path calls them, and they are removed
with the legacy objective."""

from copy import deepcopy

from ..calibration.constants import ROOM_METHOD
from ..calibration.settings import DEFAULT_AUTOMATIC_WEIGHT
from ..history import pace

METHOD = ROOM_METHOD


def _options(objective, device_id, key):
    radar = objective.radars[device_id]
    preferred = objective.preferred[device_id][key]
    allowed = radar.allowed[key]
    current = min(
        allowed, key=lambda value: (abs(value - objective.current[device_id][key]), -value)
    )
    unique = {}
    for threshold in allowed:
        bits = radar.tables[key][threshold]
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
                pace.rest()
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
    best = {
        device: {
            key: min(
                objective.radars[device].allowed[key],
                key=lambda value: (abs(value - current), -value),
            )
            for key, current in gates.items()
        }
        for device, gates in objective.current.items()
    }
    quiet = {
        device: {key: max(objective.radars[device].allowed[key]) for key in gates}
        for device, gates in best.items()
    }
    for seed in (deepcopy(best), objective.preferred, quiet):
        candidate = deepcopy(seed)
        for _ in range(4):
            if not _sweep(objective, candidate, options):
                break
        if objective.rank(candidate) < objective.rank(best):
            best = candidate
    return best


def measure(members, thresholds, start, end, room=None, *, quality=None):
    """One fixed pattern assessed on ``room`` truth by the hybrid evaluator (no refit)."""
    from ..engine.hybrid import facade

    return facade.measure(members, thresholds, start, end, room, quality=quality)


def fit(
    members,
    start,
    end,
    room=None,
    automatic_weight=DEFAULT_AUTOMATIC_WEIGHT,
    *,
    quality_contexts=None,
):
    """One zone's joint fit: exactly ``fit_set`` of that zone, without ``groups``."""
    from ..engine.hybrid import facade

    return facade.room_fit(
        members, start, end, room, automatic_weight, quality_contexts=quality_contexts
    )


def fit_set(
    groups,
    start,
    end,
    rooms=None,
    automatic_weight=DEFAULT_AUTOMATIC_WEIGHT,
    *,
    quality_contexts=None,
):
    """Fit a learning set together with the hybrid engine.

    ``groups`` is ``{group_id: members}``; ``rooms`` is ``{group_id: truth.view snapshot}``
    (missing: no room labels). Each zone is scored on its own room truth; zones without
    observed labelled time in both classes are skipped (``groups[gid] == {"skipped":
    reason}``) and the set is fitted on the rest. A radar serving only skipped zones keeps
    its current threshold and is not searched. Raises only when no zone can be scored.

    Returns ``fit``'s keys plus ``groups``. ``cost_before``/``cost_after`` are sums of the
    scored zones' common time-and-episode costs. A one-zone set is identical to ``fit``.
    For several zones the top-level ``before``/``after`` have ``room`` None and per-radar
    measures only: callers must use ``groups`` for per-zone results."""
    from ..engine.hybrid import facade

    return facade.fit_set(
        groups, start, end, rooms, automatic_weight, quality_contexts=quality_contexts
    )

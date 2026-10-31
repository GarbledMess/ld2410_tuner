"""Packed decisions for a bounded block of deduplicated episode groups.

Larger inputs use the streaming sufficient-statistic path. This is an exact
representation choice: no cell, episode or multiplicity is sampled.
"""

from bisect import bisect_right
from collections import OrderedDict
from itertools import groupby

from .constants import CELL_SECONDS, MASK_CACHE_BYTES, SHARD_CELLS
from .decision import Decision

_TABLES = tuple(bytes(49 if t < v <= 100 else 48 for v in range(256)) for t in range(101))


def _canonical(column):
    values = sorted(set(column))
    return tuple(bisect_right(values, t) for t in range(101))


class GroupBlock:
    def __init__(self, problem, store, zone, rows, attenuate):
        rid = zone.members[0]
        pattern = {r: dict.fromkeys(problem.radars[r].channels, 100) for r in zone.members}
        decision = Decision(problem, store, zone, pattern, rid, 0, attenuate)
        vectors = [decision.energies(row[4], row[0], row[1]) for row in rows]
        self.columns = tuple(bytes(c) for c in zip(*vectors, strict=True))
        self.canonical = tuple(_canonical(col) for col in self.columns)
        self.members = decision.members
        self.episodes = tuple(
            _episode(group) for _, group in groupby(enumerate(rows), key=lambda x: x[1][:3])
        )
        self.denominators = tuple(
            sum(count * conf * CELL_SECONDS * bits.bit_count() for count, conf, bits in ep[3])
            for ep in self.episodes
        )
        self.masks, self.scores = OrderedDict(), OrderedDict()
        self.used = sum(map(len, self.columns)) + sum(128 + 64 * len(ep[3]) for ep in self.episodes)
        self.limit = max(0, 8 * MASK_CACHE_BYTES - self.used)
        self.cache_bytes = 0
        self.policy = problem.policy
        self.unions = {}
        self.time_formula = None
        self.coordinate_scores = OrderedDict()

    def mask(self, coordinate, threshold):
        key = coordinate, self.canonical[coordinate][threshold]
        if key not in self.masks:
            col = self.columns[coordinate]
            value = int(col[::-1].translate(_TABLES[threshold]), 2) if col else 0
            size = (value.bit_length() + 7) // 8 + 96
            self.masks[key] = value, size
            self.cache_bytes += size
            self._prune()
            return value
        self.masks.move_to_end(key)
        return self.masks[key][0]

    def _prune(self):
        while self.masks and self.cache_bytes > self.limit:
            _, (_, size) = self.masks.popitem(last=False)
            self.cache_bytes -= size

    def base(self, pattern, rid, channel):
        result = 0
        for offset, member in enumerate(self.members):
            signature = tuple(pattern[member].get(c, 100) for c in range(18))
            if member not in self.unions or self.unions[member][0] != signature:
                self.unions[member] = self._unions(offset, signature)
            _, prefix, suffix = self.unions[member]
            result |= prefix[channel] | suffix[channel + 1] if member == rid else prefix[-1]
        return result

    def _unions(self, offset, signature):
        masks = [self.mask(offset * 18 + c, t) for c, t in enumerate(signature)]
        prefix, suffix = [0], [0] * 19
        for mask in masks:
            prefix.append(prefix[-1] | mask)
        for c in range(17, -1, -1):
            suffix[c] = suffix[c + 1] | masks[c]
        return signature, prefix, suffix

    def time_cost(self, mask, store, zone, policy):
        from .search_scores import _coefficients, linear_cost

        if self.time_formula is None:
            stats = self.statistics(0, False)
            coefficients = {
                (key, index): value for key, index, value in _coefficients(stats, policy)
            }
            self.time_formula = (
                linear_cost(store, zone, stats, policy),
                self._time_lanes(coefficients),
            )
        base, lanes = self.time_formula
        return base + sum(value * (mask & bits).bit_count() for value, bits in lanes)

    def _time_lanes(self, coefficients):
        lanes = {}
        for state, source, _mass, strata in self.episodes:
            key = state + "/" + ("human" if source == "human" else "automatic")
            factor = coefficients.get((key, 1), 0) * (-1 if state == "present" else 1)
            for count, confidence, bits in strata:
                value = factor * count * confidence * CELL_SECONDS
                lanes[value] = lanes.get(value, 0) | bits
        return tuple(lanes.items())

    def statistics(self, mask, episodes):
        result = {}
        for (state, source, mass, strata), weighted in zip(
            self.episodes, self.denominators, strict=True
        ):
            seen = sum(
                count * CELL_SECONDS * (mask & bits).bit_count() for count, conf, bits in strata
            )
            wseen = sum(
                count * conf * CELL_SECONDS * (mask & bits).bit_count()
                for count, conf, bits in strata
            )
            key = state + "/" + ("human" if source == "human" else "automatic")
            stats = result.setdefault(key, [0.0] * 4)
            stats[0] += weighted
            stats[1] += weighted - wseen if state == "present" else wseen
            if episodes:
                stats[2] += weighted / mass
                stats[3] += weighted / mass * (seen < mass / 2 if state == "present" else seen > 0)
        return result


def _episode(rows):
    strata = {}
    mass = 0
    for position, (_state, _source, _parent, confidence, _vector, count) in rows:
        key = count, confidence
        strata[key] = strata.get(key, 0) | (1 << position)
        mass += count * CELL_SECONDS
    return (
        _state,
        _source,
        mass,
        tuple((count, conf, mask) for (count, conf), mask in strata.items()),
    )


def packed_block(problem, store, zone, attenuate):
    key = zone.group_id, attenuate, id(problem.policy)
    if getattr(store, "_packed_key", None) == key:
        return store._packed_block
    cursor = store._database.execute(
        "SELECT state,source,parent,confidence,vector,n FROM search_groups WHERE zone=? ORDER BY state,source,parent,confidence,vector LIMIT ?",
        (zone.group_id, SHARD_CELLS + 1),
    )
    rows = cursor.fetchall()
    cursor.close()
    estimate = len(rows) * (18 * len(zone.members) + 256)
    block = None
    if rows and len(rows) <= SHARD_CELLS and estimate <= 2 * MASK_CACHE_BYTES:
        block = GroupBlock(problem, store, zone, rows, attenuate)
    store._packed_key, store._packed_block = key, block
    return block


def packed_costs(
    problem, store, zone, pattern, rid, channel, candidates, attenuate, *, episodes=True
):
    from .search_scores import zone_policy

    block = packed_block(problem, store, zone, attenuate)
    if block is None:
        return None
    base = block.base(pattern, rid, channel)
    position = block.members.index(rid) * 18 + channel if rid in block.members else None
    family = episodes, position, base, tuple(candidates)
    if family in block.coordinate_scores:
        return block.coordinate_scores[family]
    costs = _coordinate_scores(
        block,
        base,
        position,
        candidates,
        episodes,
        store,
        zone.group_id,
        zone_policy(problem, zone),
    )
    block.coordinate_scores[family] = costs
    while len(block.coordinate_scores) > 128:
        block.coordinate_scores.popitem(last=False)
    return costs


def _coordinate_scores(block, base, position, candidates, episodes, store, zone, policy):
    costs, evaluated = {}, {}
    for threshold in candidates:
        canonical = block.canonical[position][threshold] if position is not None else 0
        if canonical not in evaluated:
            mask = base | (block.mask(position, threshold) if position is not None else 0)
            evaluated[canonical] = _score(block, mask, episodes, store, zone, policy)
        costs[threshold] = evaluated[canonical]
    return costs


def _score(block, mask, episodes, store, zone, policy):
    from .search_scores import linear_cost

    key = episodes, mask
    if key not in block.scores:
        if episodes:
            value = linear_cost(store, zone, block.statistics(mask, True), policy)
        else:
            value = block.time_cost(mask, store, zone, policy)
        block.scores[key] = value
        while len(block.scores) > 2048:
            block.scores.popitem(last=False)
    return block.scores[key]

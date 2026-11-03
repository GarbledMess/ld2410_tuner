"""Packed decisions for a bounded block of deduplicated episode groups.

Larger inputs use the streaming sufficient-statistic path. This is an exact
representation choice: no cell, episode or multiplicity is sampled.
"""

from bisect import bisect_right
from collections import OrderedDict
from itertools import accumulate, groupby, islice, repeat
from operator import and_, itemgetter, mul, or_

from ...const import HISTORY_KEYS
from .constants import CELL_SECONDS, SHARD_CELLS

# A packed block holds deduplicated groups: one byte per member channel and one
# position bit per group, so its size follows distinct groups, not cells.
PACKED_ROWS = 2**16
PACKED_BYTES = 16 * 1024**2
PACKED_CACHE_BYTES = 32 * 1024**2
_PLANES = tuple(
    bytes(49 if v <= 100 and v & (1 << bit) else 48 for v in range(256)) for bit in range(7)
)
_TABLES = tuple(bytes(49 if t < v <= 100 else 48 for v in range(256)) for t in range(101))


def _canonical(column):
    values = sorted(set(column))
    return tuple(bisect_right(values, t) for t in range(101))


class GroupBlock:
    def __init__(self, problem, store, zone, rows, attenuate):
        self.members = tuple(sorted(zone.members))
        joined = bytearray()
        rows = _buffer_rows(rows, joined)
        self.episodes = tuple(
            _episode(group) for _, group in groupby(enumerate(rows), key=lambda x: x[1][:3])
        )
        self.columns = _energy_columns(problem, self.members, joined, self.episodes, attenuate)
        self.canonical = tuple(_canonical(col) for col in self.columns)
        self.local_episodes = tuple(
            tuple(_local_stratum(stratum) for stratum in ep[3]) for ep in self.episodes
        )
        self.denominators = tuple(
            sum(count * conf * CELL_SECONDS * bits.bit_count() for count, conf, bits in ep[3])
            for ep in self.episodes
        )
        self.masks, self.scores = OrderedDict(), OrderedDict()
        self.used = sum(map(len, self.columns)) + len(self.columns) * 4096
        self.used += sum(_episode_size(ep) for ep in self.episodes)
        self.used += sum(
            192 + (bits.bit_length() + 7) // 8 * 4 // 3
            for strata in self.local_episodes
            for _count, _confidence, _window, bits in strata
        )
        self.limit = max(0, PACKED_CACHE_BYTES - self.used)
        self.cache_bytes = 0
        self.cache_order = OrderedDict()
        self.policy = problem.policy
        self.unions = {}
        self.member_masks = OrderedDict()
        self.time_formula = None
        self.coordinate_scores = OrderedDict()
        self.planes = OrderedDict()
        self.signatures = {}

    def mask(self, coordinate, threshold):
        key = coordinate, self.canonical[coordinate][threshold]
        if key not in self.masks:
            col = self.columns[coordinate]
            value = self._crossing(coordinate, col, threshold)
            size = (value.bit_length() + 7) // 8 + 96
            self.remember(self.masks, key, value, size)
            return value
        return self.cached(self.masks, key)

    def _crossing(self, coordinate, column, threshold):
        if len(column) < 4096:
            return int(column[::-1].translate(_TABLES[threshold]), 2) if column else 0
        planes = self._planes(coordinate, column)
        result = 0
        for bit, plane in enumerate(planes):
            result = result & plane if threshold & (1 << bit) else result | plane
        return result

    def _planes(self, coordinate, column):
        if coordinate not in self.planes:
            reversed_column = column[::-1]
            planes = tuple(int(reversed_column.translate(table), 2) for table in _PLANES)
            size = 512 + sum(64 + (v.bit_length() + 7) // 8 * 4 // 3 for v in planes)
            self.remember(self.planes, coordinate, planes, size)
        else:
            planes = self.cached(self.planes, coordinate)
        return planes

    def cached(self, cache, key):
        self.cache_order.move_to_end((id(cache), key))
        return cache[key][0]

    def remember(self, cache, key, value, size):
        if key in cache:
            self.cache_bytes -= cache[key][1]
            del self.cache_order[id(cache), key]
        cache[key] = value, size
        self.cache_order[id(cache), key] = cache, size
        self.cache_bytes += size
        self._trim()

    def _trim(self):
        while self.cache_bytes > self.limit:
            (_identity, old), (entries, removed) = self.cache_order.popitem(last=False)
            del entries[old]
            self.cache_bytes -= removed

    def base(self, pattern, rid, channel):
        signatures, result = self._other_members(pattern, rid)
        if rid not in self.members:
            return result
        offset = self.members.index(rid)
        _, prefix, suffix = self._member_union(offset, signatures[offset])
        return result | prefix[channel] | suffix[channel + 1]

    def _member_union(self, offset, signature):
        member = self.members[offset]
        found = self.unions.get(member)
        if found is None or found[0][0] != signature:
            union = self._unions(offset, signature)
            self.remember(self.unions, member, union, _union_size(union))
            return union
        return self.cached(self.unions, member)

    def _other_members(self, pattern, rid):
        signatures = tuple(self._signature(member, pattern[member]) for member in self.members)
        found = self.member_masks.get(0)
        if found is not None and all(
            a == b or member == rid
            for member, a, b in zip(self.members, found[0][0], signatures, strict=True)
        ):
            _, prefix, suffix = self.cached(self.member_masks, 0)
        else:
            masks = [
                self._member_union(i, signature)[1][-1] for i, signature in enumerate(signatures)
            ]
            prefix = list(accumulate(masks, or_, initial=0))
            suffix = list(accumulate(reversed(masks), or_, initial=0))[::-1]
            union = signatures, prefix, suffix
            size = _union_size(union) + sum(1024 + 64 * len(s) for s in signatures)
            self.remember(self.member_masks, 0, union, size)
        if rid not in self.members:
            return signatures, prefix[-1]
        at = self.members.index(rid)
        return signatures, prefix[at] | suffix[at + 1]

    def _signature(self, member, settings):
        method = getattr(settings, "signature", None)
        key = method() if method is not None else tuple(settings.items())
        found = self.signatures.get(member)
        if found is None or found[0] != key:
            found = key, tuple(map(settings.get, range(18), repeat(100)))
            self.signatures[member] = found
        return found[1]

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

        formula = self.time_formula
        if formula is None:
            stats = self.statistics(0, False)
            coefficients = {
                (key, index): value for key, index, value in _coefficients(stats, policy)
            }
            formula = linear_cost(store, zone, stats, policy), self._time_lanes(coefficients)
            self._retain_formula(formula)
        base, (values, bits) = formula
        return base + sum(map(mul, values, map(int.bit_count, map(and_, repeat(mask), bits))))

    def conditional_times(self, base, position, candidates, store, zone, policy):
        self.time_cost(0, store, zone, policy)
        if self.time_formula is None:
            return None
        intercept, (factors, bits) = self.time_formula
        column = self.columns[position]
        planes = self._planes(position, column)
        counts = tuple(_plane_counts(planes, lane, base) for lane in bits)
        return {
            t: intercept
            + sum(
                factor * (fixed + tail[100 - t])
                for factor, (fixed, tail) in zip(factors, counts, strict=True)
            )
            for t in candidates
        }

    def _retain_formula(self, formula):
        size = 256 + sum(160 + (v.bit_length() + 7) // 8 * 4 // 3 for v in formula[1][1])
        if self.used + size <= PACKED_CACHE_BYTES:
            self.time_formula = formula
            self.used += size
            self.limit = PACKED_CACHE_BYTES - self.used
            self._trim()

    def _time_lanes(self, coefficients):
        entries = []
        groups = {}
        for state, source, _mass, strata in self.episodes:
            key = state + "/" + ("human" if source == "human" else "automatic")
            factor = coefficients.get((key, 1), 0) * (-1 if state == "present" else 1)
            for count, confidence, bits in strata:
                entries.append((factor, count, confidence, bits))
                counts = groups.setdefault((factor, confidence), {})
                counts[count] = counts.get(count, 0) | bits
        return _time_lanes(entries, groups)

    def statistics(self, mask, episodes):
        result = {}
        pool = mask.to_bytes((mask.bit_length() + 7) // 8, "little")
        for (state, source, mass, _strata), weighted, local in zip(
            self.episodes, self.denominators, self.local_episodes, strict=True
        ):
            seen, wseen = _seen_mass(pool, local)
            key = state + "/" + ("human" if source == "human" else "automatic")
            stats = result.setdefault(key, [0.0] * 4)
            stats[0] += weighted
            stats[1] += weighted - wseen if state == "present" else wseen
            if episodes:
                stats[2] += weighted / mass
                stats[3] += weighted / mass * (seen < mass / 2 if state == "present" else seen > 0)
        return result


def _seen_mass(pool, local):
    if len(local) == 1:
        count, confidence, window, bits = local[0]
        hits = (int.from_bytes(pool[window], "little") & bits).bit_count()
        return count * CELL_SECONDS * hits, count * confidence * CELL_SECONDS * hits
    counts = tuple(
        (count, confidence, (int.from_bytes(pool[window], "little") & bits).bit_count())
        for count, confidence, window, bits in local
    )
    return (
        sum(count * CELL_SECONDS * hits for count, _confidence, hits in counts),
        sum(count * confidence * CELL_SECONDS * hits for count, confidence, hits in counts),
    )


def _plane_counts(planes, lane, base):
    bins = [0] * 102
    _partition_counts(planes, len(planes) - 1, lane & ~base, 0, bins)
    return (lane & base).bit_count(), tuple(accumulate(reversed(bins)))


def _partition_counts(planes, bit, selected, value, bins):
    if not selected or value > 100:
        return
    if bit < 0:
        bins[value] = selected.bit_count()
        return
    high = selected & planes[bit]
    _partition_counts(planes, bit - 1, selected ^ high, value, bins)
    _partition_counts(planes, bit - 1, high, value | (1 << bit), bins)


def _local_stratum(stratum):
    count, confidence, bits = stratum
    start = ((bits & -bits).bit_length() - 1) >> 3
    window = slice(start, (bits.bit_length() + 7) >> 3)
    return count, confidence, window, bits >> (start * 8)


def _union_size(union):
    _signature, prefix, suffix = union
    return 1024 + sum(64 + (v.bit_length() + 7) // 8 * 4 // 3 for v in prefix + suffix)


def _time_lanes(entries, groups):
    lanes, emitted = {}, set()
    for factor, count, confidence, bits in entries:
        key = factor, confidence
        counts = groups[key]
        if len(counts) < max(counts).bit_length():
            value = factor * count * confidence * CELL_SECONDS
            lanes[value] = lanes.get(value, 0) | bits
        elif key not in emitted:
            emitted.add(key)
            _weight_lanes(lanes, factor * confidence * CELL_SECONDS, counts)
    return tuple(lanes), tuple(lanes.values())


def _weight_lanes(lanes, factor, counts):
    for count, bits in counts.items():
        while count:
            weight = count & -count
            value = factor * weight
            lanes[value] = lanes.get(value, 0) | bits
            count -= weight


def _episode(rows):
    strata = {}
    mass, start = 0, None
    for position, (_state, _source, _parent, confidence, _vector, count) in rows:
        start = position if start is None else start
        key = count, confidence
        strata[key] = strata.get(key, 0) | (1 << (position - start))
        mass += count * CELL_SECONDS
    return (
        _state,
        _source,
        mass,
        tuple((count, conf, mask << start) for (count, conf), mask in strata.items()),
    )


def _buffer_rows(rows, joined):
    rows = iter(rows)
    while batch := tuple(islice(rows, SHARD_CELLS)):
        joined.extend(b"".join(map(itemgetter(4), batch)))
        yield from batch


def _energy_columns(problem, members, joined, episodes, attenuate):
    width, result = 18 * len(members), []
    windows, start = [], 0
    for state, source, _mass, strata in episodes:
        end = max(mask.bit_length() for _count, _confidence, mask in strata)
        windows.append((start, end, state == "present", source == "human"))
        start = end
    for at in range(width):
        member, channel = members[at // 18], at % 18
        column = bytearray(joined[at::width])
        tables = {}
        for start, end, present, human in windows:
            key = present, human
            if key not in tables:
                tables[key] = _energy_table(problem, member, channel, present, human, attenuate)
            column[start:end] = column[start:end].translate(tables[key])
        result.append(bytes(column))
    return tuple(result)


def _energy_table(problem, member, channel, present, human, attenuate):
    model = problem.policy.quality.get("models", {}).get(member) if present and not human else None
    values = [3 * v // 5 if present and attenuate else v for v in range(101)] + [0] * 155
    if model is not None:
        values = [
            value if model.supports(HISTORY_KEYS[channel], v) else 0
            for v, value in enumerate(values)
        ]
    return bytes(values)


def _episode_size(episode):
    return 256 + sum(192 + (mask.bit_length() + 7) // 8 * 4 // 3 for _, _, mask in episode[3])


def _packed_size(store, zone):
    """Conservative columns, canonical tables, episodes and global bit-plane bytes."""
    rows = store._database.execute(
        "SELECT state,source,parent,sum(rows),count(*) FROM ("
        "SELECT state,source,parent,confidence,n,count(*) AS rows FROM search_groups "
        "WHERE zone=? GROUP BY state,source,parent,confidence,n) "
        "GROUP BY state,source,parent ORDER BY state,source,parent",
        (zone.group_id,),
    )
    count, size = 0, 18 * len(zone.members) * 4096
    for _state, _source, _parent, n, strata in rows:
        count += n
        size += 256 + strata * (384 + (count + n + 14) // 8 * 4 // 3)
    return count, size + count * 18 * len(zone.members)


def packed_block(problem, store, zone, attenuate):
    key = zone.group_id, attenuate, id(problem.policy)
    if getattr(store, "_packed_key", None) == key:
        return store._packed_block
    count, estimate = _packed_size(store, zone)
    block = None
    if count and estimate <= PACKED_BYTES:
        cursor = store._database.execute(
            "SELECT state,source,parent,confidence,vector,n FROM search_groups "
            "WHERE zone=? ORDER BY state,source,parent,confidence,vector",
            (zone.group_id,),
        )
        try:
            block = GroupBlock(problem, store, zone, cursor, attenuate)
        finally:
            cursor.close()
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
        return block.cached(block.coordinate_scores, family)
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
    size = 512 + len(costs) * 128 + (base.bit_length() + 7) // 8 * 4 // 3
    block.remember(block.coordinate_scores, family, costs, size)
    return costs


def _coordinate_scores(block, base, position, candidates, episodes, store, zone, policy):
    if not candidates:
        return {}
    lowest = block.mask(position, min(candidates)) if position is not None else 0
    if not lowest & ~base:
        return dict.fromkeys(candidates, _score(block, base, episodes, store, zone, policy))
    if (
        not episodes
        and position is not None
        and len(candidates) > 8
        and len(block.columns[position]) >= 65536
    ):
        costs = block.conditional_times(base, position, candidates, store, zone, policy)
        if costs is not None:
            return costs
    return _varying_scores(block, base, position, candidates, episodes, store, zone, policy)


def _varying_scores(block, base, position, candidates, episodes, store, zone, policy):
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
        size = 256 + (mask.bit_length() + 7) // 8 * 4 // 3
        block.remember(block.scores, key, value, size)
        return value
    return block.cached(block.scores, key)

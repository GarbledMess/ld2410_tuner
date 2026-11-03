"""Global coordinate search using lossless disk-backed decision groups."""

import sys
from array import array
from bisect import bisect_right
from collections import OrderedDict
from heapq import nsmallest
from math import ceil
from operator import getitem

from ...history import pace
from .candidates import candidate_values, channel_statistics, preferred
from .constants import CELL_SECONDS, MASK_CACHE_BYTES, SHORTLIST, SWEEPS
from .domains import normalize_pattern
from .search_data import collect_training
from .search_scores import conditional_costs, coordinate_costs

__all__ = [
    "collect_training",
    "conditional_histograms",
    "coordinate_costs",
    "quiet_candidates",
    "run_search",
    "search",
]


def conditional_histograms(channels, covered, weights):
    """Weighted strict-crossing histogram of one coordinate, excluding its OR.

    Channels is a one-column tuple, matching the shard's column layout. Byte
    translation handles the common equal-weight lane in C; arbitrary corrected
    confidence uses bounded scalar accumulation over the same positions.
    """
    if len(channels) != 1 or any(len(w) != len(covered) for w in weights):
        raise ValueError("Conditional histogram shape mismatch")
    column = channels[0]
    if len(column) != len(covered):
        raise ValueError("Conditional histogram shape mismatch")
    result = []
    for weight in weights:
        result.append(_weighted_histogram(column, covered, weight))
    return tuple(result)


def _weighted_histogram(column, covered, weights):
    bins = [0.0] * 101
    for value, used, weight in zip(column, covered, weights, strict=True):
        if not used and value <= 100:
            bins[value] += weight
    return bins


def _key(pattern, coordinates):
    return bytes(pattern[rid][c] for rid, c in coordinates)


def _copy(pattern):
    return {rid: dict(values) for rid, values in pattern.items()}


class _Thresholds(dict):
    """Search-local member settings with a signature invalidated by each coordinate."""

    def __init__(self, values, changed=None):
        super().__init__(values)
        self._signature = None
        self._changed = changed

    def __setitem__(self, channel, value):
        if self.get(channel) != value:
            self._signature = None
            if self._changed is not None:
                self._changed(channel, value)
        super().__setitem__(channel, value)

    def signature(self):
        if self._signature is None:
            self._signature = tuple(self.items())
        return self._signature


class _Pattern(dict):
    """Running search thresholds, tracking only members whose values changed."""

    def __init__(self, values, engine):
        self.engine = engine
        self.dirty = dict.fromkeys(values)
        self.crossings = {}
        self.parts = [b""] * len(engine.member_coordinates)
        self.family = b""
        self.threshold_key = bytearray(_key(values, engine.coordinates))
        self.threshold_bytes = None
        super().__init__(
            (rid, _Thresholds(settings, lambda c, v, rid=rid: self._change(rid, c, v)))
            for rid, settings in values.items()
        )

    def _change(self, rid, channel, value):
        self.dirty.setdefault(rid)
        self.threshold_key[self.engine.key_positions[rid, channel]] = value
        self.threshold_bytes = None

    def key(self):
        if self.threshold_bytes is None:
            self.threshold_bytes = bytes(self.threshold_key)
        return self.threshold_bytes


def _memo_family(family, largest):
    """Losslessly pack small member IDs; retain all 64 bits after ID growth."""
    if largest >= 2**16:
        return b"\1" + family
    low, high = (0, 1) if sys.byteorder == "little" else (7, 6)
    return b"\0" + family[low::8] + family[high::8]


def _candidate_costs(order, payload, candidates, crossings):
    costs = dict(zip(order, payload, strict=True))
    return dict(zip(candidates, map(costs.__getitem__, crossings), strict=True))


def _candidate_layout(table, candidates):
    crossings = bytes(map(table.__getitem__, candidates))
    order = bytes(sorted(set(crossings)))
    thresholds = bytes(candidates[crossings.index(crossing)] for crossing in order)
    return crossings, order, thresholds


class _Search:
    def __init__(self, problem, training, attenuate=False):
        self.problem, self.training, self.attenuate = problem, training, attenuate
        self.start = normalize_pattern(problem.radars)
        self.stats = self._statistics()
        self.coordinates = self._coordinates()
        self.key_members = tuple(rid for rid, _c in self.coordinates)
        self.key_channels = tuple(c for _rid, c in self.coordinates)
        self.key_positions = {item: i for i, item in enumerate(self.coordinates)}
        self.candidate_base = {
            (rid, c): candidate_values(
                problem.radars[rid].domains[c],
                problem.radars[rid].domains[c][-1],
                self.stats[rid, c],
                {},
            )
            for rid, c in self.coordinates
        }
        self.pool = {}
        self.ties = {}
        self.memo = OrderedDict()
        self.memo_bytes = 0
        self.layouts, self.layout_bytes = OrderedDict(), 0
        self.families = {}
        self.family_ids, self.family_bytes, self.next_family = {}, 0, 0
        self.member_coordinates = {
            rid: tuple(item for item in self.coordinates if item[0] == rid)
            for rid in self.problem.radars
        }
        self.member_positions = {member: i for i, member in enumerate(self.member_coordinates)}
        self.positions = {
            item: i for items in self.member_coordinates.values() for i, item in enumerate(items)
        }
        self.energies = self._all_energies()

    def _all_energies(self):
        if any(
            any(r.timing.get(k, 0) for k in ("timeout", "off_delay"))
            for r in self.problem.radars.values()
        ):
            return self._timed_energies()
        result = {item: set() for item in self.coordinates}
        for zone in self.problem.zones:
            members = tuple(sorted(zone.members))
            positions = [
                (item, members.index(item[0]) * 18 + item[1])
                for item in self.coordinates
                if item[0] in members
            ]
            self._zone_energies(zone, positions, result)
        if self.attenuate:
            result = {
                item: values | {3 * v // 5 for v in values} for item, values in result.items()
            }
        return {item: tuple(sorted(values)) for item, values in result.items()}

    def _timed_energies(self):
        from .search_timing import Geometry

        if not hasattr(self.training, "_timing_geometry"):
            self.training._timing_geometry = Geometry(self.problem, self.training)
        result = {}
        for item in self.coordinates:
            canonical = self.training._timing_geometry.canonical.get(item, (0,) * 101)
            values = {v for v in range(101) if canonical[v] != (canonical[v - 1] if v else 0)}
            if self.attenuate:
                values |= {3 * v // 5 for v in values}
            result[item] = tuple(sorted(values))
        return result

    def _zone_energies(self, zone, positions, result):
        if self._cached_zone_energies(zone, positions, result):
            return
        rows = self.training._database.execute(
            "SELECT vector FROM search_vectors WHERE zone=?", (zone.group_id,)
        )
        joined, width = bytearray(), None
        for (vector,) in rows:
            width = len(vector)
            joined += vector
            if len(joined) >= HISTOGRAM_BYTES:
                _observed(joined, width, positions, result)
                joined = bytearray()
        if joined:
            _observed(joined, width, positions, result)

    def _cached_zone_energies(self, zone, positions, result):
        if len(zone.members) == 1:
            cached = getattr(self.training, "_raw_energies", {}).get(zone.members[0])
            if cached is not None and all(item[1] in cached[0] for item, _at in positions):
                for item, _at in positions:
                    result[item].update(cached[0][item[1]])
                return True
        return False

    def _statistics(self):
        return statistics(self.problem, self.training, attenuate=self.attenuate)

    def _coordinates(self):
        # Data-defined order avoids using radar names as an optimization policy.
        def signature(item):
            rid, c = item
            stat = self.stats[item]
            return (tuple(stat["hp"]), tuple(stat["he"]), self.problem.radars[rid].domains[c], c)

        return tuple(sorted(self.stats, key=signature))

    def cost(self, pattern):
        if not self.coordinates:
            return 0
        rid, c = self.coordinates[0]
        return self.costs(pattern, rid, c, (pattern[rid][c],))[pattern[rid][c]]

    def costs(self, pattern, rid, c, candidates):
        return self._memoized("score", pattern, rid, c, candidates, coordinate_costs)

    def _family(self, pattern, rid, c):
        if isinstance(pattern, _Pattern) and pattern.engine is self:
            return self._running_family(pattern, rid, c)
        return self._plain_family(pattern, rid, c)

    def _plain_family(self, pattern, rid, c):
        parts = []
        for member, items in self.member_coordinates.items():
            values = pattern[member]
            signature = (
                values.signature() if isinstance(values, _Thresholds) else tuple(values.items())
            )
            key = member, signature
            if key not in self.families:
                self.families[key] = bytes(
                    bisect_right(self.energies[item], values[item[1]]) for item in items
                )
                if len(self.families) > 256:
                    self.families.pop(next(iter(self.families)))
            crossing = self.families[key]
            if member == rid:
                crossing = bytearray(crossing)
                crossing[self.positions[rid, c]] = 255
                crossing = bytes(crossing)
            parts.append(self._family_id(member, crossing))
        return array("Q", parts).tobytes()

    def _running_family(self, pattern, rid, c):
        changed = not pattern.family
        for member in pattern.dirty:
            values = pattern[member]
            crossing = bytes(
                bisect_right(self.energies[item], values[item[1]])
                for item in self.member_coordinates[member]
            )
            if crossing != pattern.crossings.get(member):
                pattern.crossings[member] = crossing
                value = self._family_id(member, crossing)
                pattern.parts[self.member_positions[member]] = array("Q", (value,)).tobytes()
                changed = True
        pattern.dirty.clear()
        if changed:
            pattern.family = b"".join(pattern.parts)
        crossing = bytearray(pattern.crossings[rid])
        crossing[self.positions[rid, c]] = 255
        value = self._family_id(rid, bytes(crossing))
        part = array("Q", (value,)).tobytes()
        at = self.member_positions[rid] * 8
        return pattern.family[:at] + part + pattern.family[at + 8 :]

    def _family_id(self, member, crossing):
        key = member, crossing
        value = self.family_ids.get(key)
        if value is None:
            self.next_family += 1
            value = self.next_family
            self.family_ids[key] = value
            self.family_bytes += len(member.encode()) + len(crossing) + 256
            while self.family_bytes > MASK_CACHE_BYTES:
                old = next(iter(self.family_ids))
                self.family_bytes -= len(old[0].encode()) + len(old[1]) + 256
                del self.family_ids[old]
        return value

    def _layout(self, rid, c, candidates):
        item = rid, c
        domain = self.problem.radars[rid].domains[c]
        cached = self.layouts.get(item)
        if cached is None:
            table = bytes(bisect_right(self.energies[item], t) for t in range(101))
            layout = _candidate_layout(table, domain)
            size = len(rid.encode()) + len(table) + sum(map(len, layout)) + 1024
            self.layouts[item] = table, layout, size
            self.layout_bytes += size
            while self.layout_bytes > MASK_CACHE_BYTES:
                _key, (_table, _layout, removed) = self.layouts.popitem(last=False)
                self.layout_bytes -= removed
        else:
            table, layout, _size = cached
            self.layouts.move_to_end(item)
        return layout if candidates == domain else _candidate_layout(table, candidates)

    def _memoized(self, mode, pattern, rid, c, candidates, function):
        family = _memo_family(self._family(pattern, rid, c), self.next_family)
        crossings, order, thresholds = self._layout(rid, c, candidates)
        key = mode, rid, c, family, order
        if key not in self.memo:
            result = function(
                self.problem, self.training, pattern, rid, c, thresholds, attenuate=self.attenuate
            )
            payload = array("d", (result[t] for t in thresholds))
            size = len(family) + 512 + len(order) + payload.itemsize * len(payload)
            self.memo[key] = payload, size
            self.memo_bytes += size
            while self.memo_bytes > MASK_CACHE_BYTES:
                _, (_, removed) = self.memo.popitem(last=False)
                self.memo_bytes -= removed
            return _candidate_costs(order, payload, candidates, crossings)
        self.memo.move_to_end(key)
        return _candidate_costs(order, self.memo[key][0], candidates, crossings)

    def remember(self, pattern, cost):
        key = self.pattern_key(pattern)
        self.pool[key] = cost, _copy(pattern)
        if len(self.pool) > 128:
            retained = sorted(self.pool.items(), key=lambda item: (item[1][0], item[0]))[:64]
            self.pool = dict(retained)

    def pattern_key(self, pattern):
        if isinstance(pattern, _Pattern) and pattern.engine is self:
            return pattern.key()
        return bytes(map(getitem, map(pattern.__getitem__, self.key_members), self.key_channels))

    def _rank(self, rid, c, threshold, cost):
        stat = self.stats[rid, c]
        gap = stat["gap"]
        margin = min(threshold - gap[0], gap[1] - threshold) if gap else 0
        below = stat["ceiling"] is not None and threshold < stat["ceiling"]
        return round(cost, 9), below, -margin, threshold != self.start[rid][c], threshold

    def coordinate(self, pattern, rid, c):
        stat = self.stats[rid, c]
        if not sum(stat["hp"]):
            return False
        domain = self.problem.radars[rid].domains[c]
        tails = self._memoized("tail", pattern, rid, c, domain, conditional_costs)
        candidates = tuple(
            sorted(
                set(self.candidate_base[rid, c])
                | {pattern[rid][c]}
                | set(nsmallest(4, tails, key=lambda t: (tails[t], t)))
            )
        )
        costs = self.costs(pattern, rid, c, candidates)
        best = min(candidates, key=lambda t: self._rank(rid, c, t, costs[t]))
        self._lowered_tie(rid, c, best, costs)
        changed = best != pattern[rid][c]
        pattern[rid][c] = best
        self.remember(pattern, costs[best])
        return changed

    def _lowered_tie(self, rid, c, best, costs):
        ceiling = self.stats[rid, c]["ceiling"]
        lower = [
            t
            for t in costs
            if t < best
            and round(costs[t], 9) == round(costs[best], 9)
            and (ceiling is None or t >= ceiling)
        ]
        if lower:
            self.ties[rid, c] = min(lower)
        else:
            self.ties.pop((rid, c), None)

    def run(self, seed, sweeps):
        pattern = _Pattern(seed, self)
        self.remember(pattern, self.cost(pattern))
        for _ in range(sweeps):
            changes = [self.coordinate(pattern, rid, c) for rid, c in self.coordinates]
            pace.rest()
            if not any(changes):
                break
        return _copy(pattern)

    def seeds(self):
        centre, quiet = _copy(self.start), _copy(self.start)
        for rid, c in self.coordinates:
            domain = self.problem.radars[rid].domains[c]
            centre[rid][c] = preferred(domain, self.start[rid][c], self.stats[rid, c])
            quiet[rid][c] = domain[-1]
        return self.start, centre, quiet

    def finalists(self, seeds, count):
        ordered = sorted(self.pool.values(), key=lambda item: (item[0], self.pattern_key(item[1])))
        base = [self.start, *seeds, *(pat for _, pat in ordered[:count])]
        variants = self._variants(ordered[0][1]) if ordered else []
        return _unique([self.repair(pat) for pat in (*base, *variants)], self.coordinates)

    def repair(self, pattern):
        return repair(pattern, self.stats, self.start)

    def _variants(self, pattern):
        combined = _copy(pattern)
        changed = []
        drops = sorted(
            (value - pattern[rid][c], rid, c, value) for (rid, c), value in self.ties.items()
        )
        for _drop, rid, c, value in drops[:8]:
            if value < pattern[rid][c]:
                variant = _copy(pattern)
                variant[rid][c] = value
                changed.append(variant)
                combined[rid][c] = value
        return [*changed[:8], combined] if changed else []


def statistics(problem, store, *, attenuate=False, masks=None):
    """Per-coordinate class histograms and gap statistics of the pure training cells.

    ``masks`` (``{zone: intervals}``) drops audited cells, so a masked re-score sees
    exactly the training evidence a masked collection would, without re-collecting.
    """
    return channel_views(class_bins(problem, store, masks), attenuate)


def class_bins(problem, store, masks=None):
    """Per coordinate and class, per-source duration histograms (one pass)."""
    bins = {
        item: {state: {} for state in ("present", "not_present")}
        for rid, r in problem.radars.items()
        for item in ((rid, c) for c in r.channels)
    }
    for zone in problem.zones:
        members = tuple(sorted(zone.members))
        positions = [
            (item, members.index(item[0]) * 18 + item[1]) for item in bins if item[0] in members
        ]
        mask = None if masks is None else masks.get(zone.group_id, ())
        _merge_histograms(bins, _zone_class_bins(store, zone, positions), 1)
        if mask:
            removed = {item: {"present": {}, "not_present": {}} for item, _at in positions}
            _zone_histograms(_removed_rows(store, zone, mask), positions, removed)
            _merge_histograms(bins, removed, -1)
    _prune_histograms(bins)
    return bins


def _prune_histograms(bins):
    for states in bins.values():
        for sources in states.values():
            for source in tuple(sources):
                if not any(sources[source]):
                    del sources[source]


def _merge_histograms(target, source, sign):
    for item, states in source.items():
        for state, sources in states.items():
            for name, values in sources.items():
                hist = target[item][state].setdefault(name, [0.0] * 101)
                for value, mass in enumerate(values):
                    hist[value] += sign * mass


def _zone_class_bins(store, zone, positions):
    if not hasattr(store, "_histogram_zones"):
        store._histogram_zones, store._histogram_bytes = OrderedDict(), 0
    key = zone.group_id, tuple(positions)
    cache = store._histogram_zones
    if key in cache:
        cache.move_to_end(key)
        return cache[key][0]
    bins = {item: {"present": {}, "not_present": {}} for item, _at in positions}
    _zone_histograms(_zone_rows(store, zone, None), positions, bins)
    size = (
        8192
        + len(zone.group_id.encode())
        + sum(
            1024
            + len(item[0].encode())
            + sum(4096 + len(name.encode()) for sources in states.values() for name in sources)
            for item, states in bins.items()
        )
    )
    cache[key] = bins, size
    store._histogram_bytes += size
    while store._histogram_bytes > HISTOGRAM_BYTES:
        _old, (_bins, removed) = cache.popitem(last=False)
        store._histogram_bytes -= removed
    return bins


def _removed_rows(store, zone, mask):
    start, stop = min(a for a, _b in mask), max(b for _a, b in mask)
    rows = store._database.execute(
        "SELECT state,source,vector,t,n FROM search_pure WHERE zone=? AND t<? "
        "AND t>=?-(SELECT max_n-1 FROM search_bounds WHERE zone=?)*? AND t+(n-1)*?>=?",
        (zone.group_id, stop, start, zone.group_id, CELL_SECONDS, CELL_SECONDS, start),
    )
    for state, source, vector, t, n in rows:
        removed = masked_cells(t, n, mask)
        if removed:
            yield state, source, vector, removed


def channel_views(bins, attenuate=False):
    """Gap statistics from ``class_bins``; attenuation transforms only occupied mass."""
    from .search_data import _primary_bins

    result = {}
    for item, states in bins.items():
        hp, he = (_primary_bins(states[state]) for state in ("present", "not_present"))
        result[item] = channel_statistics(_attenuated_histogram(hp) if attenuate else hp, he)
    return result


def _zone_rows(store, zone, mask):
    if mask is None:
        yield from store._database.execute(
            "SELECT state,source,vector,n FROM search_groups WHERE zone=?", (zone.group_id,)
        )
        return
    rows = store._database.execute(
        "SELECT state,source,vector,t,n FROM search_pure WHERE zone=?", (zone.group_id,)
    )
    for state, source, vector, t, n in rows:
        kept = n - masked_cells(t, n, mask)
        if kept:
            yield state, source, vector, kept


def masked_cells(t, n, mask):
    """Cells of the pure run ``[t, t + 6n)`` whose start lies inside ``mask``."""
    total = 0
    for a, b in mask:
        lo = max(0, ceil((a - t) / CELL_SECONDS))
        hi = min(n, ceil((b - t) / CELL_SECONDS))
        total += max(0, hi - lo)
    return total


HISTOGRAM_BYTES = 8 * 1024**2


def _zone_histograms(rows, positions, bins):
    """Strided byte columns per (state, source, multiplicity), counted in C.

    Bins hold integer-valued seconds, so the result does not depend on order.
    Joined vectors are flushed every ``HISTOGRAM_BYTES``.
    """
    groups, size = {}, 0
    for state, source, vector, count in rows:
        groups.setdefault((state, source, count, len(vector)), bytearray()).extend(vector)
        size += len(vector)
        if size >= HISTOGRAM_BYTES:
            _count_groups(groups, positions, bins)
            groups, size = {}, 0
    _count_groups(groups, positions, bins)


def _observed(joined, width, positions, result):
    """Add each position's observed energies (missing excluded), read in C."""
    for item, at in positions:
        result[item].update(v for v in set(joined[at::width]) if v <= 100)


def _count_groups(groups, positions, bins):
    for (state, source, count, width), joined in groups.items():
        for item, at in positions:
            column = bytes(joined[at::width])
            values = sorted(value for value in set(column) if value <= 100)
            if values:
                hist = bins[item][state].setdefault(source, [0.0] * 101)
                for value in values:
                    hist[value] += column.count(value) * count * CELL_SECONDS


def repair(pattern, stats, start):
    """Keep the start on coordinates whose move crosses no training energy and no gap."""
    result = _copy(pattern)
    for (rid, c), stat in stats.items():
        current, value = start[rid][c], result[rid][c]
        if stat["gap"] is None and not _changes_crossing(stat, current, value):
            result[rid][c] = current
    return result


def _attenuated_histogram(hist):
    out = [0.0] * 101
    for energy, mass in enumerate(hist):
        out[3 * energy // 5] += mass
    return out


def _changes_crossing(stat, current, value):
    lo, hi = sorted((current, value))
    return any(stat["hp"][lo + 1 : hi + 1]) or any(stat["he"][lo + 1 : hi + 1])


def _unique(patterns, coordinates):
    found = set()
    result = []
    for pattern in patterns:
        key = _key(pattern, coordinates)
        if key not in found:
            found.add(key)
            result.append(pattern)
    return result


def run_search(problem, training, *, attenuate=False, sweeps=SWEEPS, shortlist=SHORTLIST):
    """The search engine (statistics, pool, repair) and its start-first finalists."""
    engine = _Search(problem, training, attenuate)
    seeds = engine.seeds()
    finals = [engine.run(seed, sweeps) for seed in seeds]
    return engine, engine.finalists((*seeds[1:], *finals), shortlist)


def search(problem, training, *, attenuate=False, sweeps=SWEEPS, shortlist=SHORTLIST):
    return run_search(problem, training, attenuate=attenuate, sweeps=sweeps, shortlist=shortlist)[1]


def best_pooled(engine):
    """The best untimed pattern seen by a search (deterministic tie order)."""
    if not engine.pool:
        return None
    return min(engine.pool.values(), key=lambda item: (item[0], engine.pattern_key(item[1])))[1]


def quiet_candidates(problem, training, engine, *, sweeps, shortlist):
    """One attenuated sweep from the best pattern plus its best pooled variants."""
    top = best_pooled(engine)
    if top is None:
        return []
    quiet = _Search(problem, training, attenuate=True)
    found = quiet.run(top, sweeps)
    ranked = sorted(quiet.pool.values(), key=lambda item: (item[0], quiet.pattern_key(item[1])))
    variants = [found, *(pattern for _, pattern in ranked[:shortlist])]
    return _unique([engine.repair(pattern) for pattern in variants], engine.coordinates)

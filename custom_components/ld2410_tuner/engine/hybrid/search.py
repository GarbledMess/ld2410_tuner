"""Global coordinate search using lossless disk-backed decision groups."""

from bisect import bisect_right
from collections import OrderedDict

from ...history import pace
from .candidates import candidate_values, channel_statistics, preferred
from .constants import MASK_CACHE_BYTES, SHORTLIST, SWEEPS
from .domains import normalize_pattern
from .search_data import collect_training
from .search_scores import conditional_costs, coordinate_costs

__all__ = ["collect_training", "conditional_histograms", "coordinate_costs", "search"]


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


class _Search:
    def __init__(self, problem, training, attenuate=False):
        self.problem, self.training, self.attenuate = problem, training, attenuate
        self.start = normalize_pattern(problem.radars)
        self.stats = self._statistics()
        self.coordinates = self._coordinates()
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
        self.families = {}
        self.positions = {item: i for i, item in enumerate(self.coordinates)}
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
        rows = self.training._database.execute(
            "SELECT DISTINCT vector FROM search_vectors WHERE zone=?", (zone.group_id,)
        )
        for (vector,) in rows:
            for item, at in positions:
                if vector[at] <= 100:
                    result[item].add(vector[at])

    def _statistics(self):
        from .search_data import _primary_bins

        bins = {
            item: {state: {} for state in ("present", "not_present")}
            for rid, r in self.problem.radars.items()
            for item in ((rid, c) for c in r.channels)
        }
        for zone in self.problem.zones:
            members = tuple(sorted(zone.members))
            positions = [
                (item, members.index(item[0]) * 18 + item[1]) for item in bins if item[0] in members
            ]
            self._zone_histograms(zone, positions, bins)
        result = {}
        for item, states in bins.items():
            hp, he = (_primary_bins(states[state]) for state in ("present", "not_present"))
            result[item] = channel_statistics(
                _attenuated_histogram(hp) if self.attenuate else hp, he
            )
        return result

    def _zone_histograms(self, zone, positions, bins):
        rows = self.training._database.execute(
            "SELECT state,source,vector,n FROM search_groups WHERE zone=?", (zone.group_id,)
        )
        for state, source, vector, count in rows:
            for item, at in positions:
                if vector[at] <= 100:
                    hist = bins[item][state].setdefault(source, [0.0] * 101)
                    hist[vector[at]] += count * 6

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

    def _memoized(self, mode, pattern, rid, c, candidates, function):
        parts = []
        for member, values in pattern.items():
            signature = tuple(values.items())
            key = member, signature
            if key not in self.families:
                self.families[key] = tuple(
                    (self.positions[item], bisect_right(self.energies[item], values[item[1]]))
                    for item in self.coordinates
                    if item[0] == member
                )
                if len(self.families) > 256:
                    self.families.pop(next(iter(self.families)))
            parts.extend(self.families[key])
        family = bytearray(len(self.coordinates))
        for position, value in parts:
            family[position] = value
        family[self.positions[rid, c]] = 255
        family = bytes(family)
        key = mode, rid, c, family, tuple(candidates)
        if key not in self.memo:
            result = function(
                self.problem, self.training, pattern, rid, c, candidates, attenuate=self.attenuate
            )
            size = len(family) + 256 + 64 * len(result)
            self.memo[key] = result, size
            self.memo_bytes += size
            while self.memo_bytes > MASK_CACHE_BYTES:
                _, (_, removed) = self.memo.popitem(last=False)
                self.memo_bytes -= removed
            return result
        self.memo.move_to_end(key)
        return self.memo[key][0]

    def remember(self, pattern, cost):
        key = _key(pattern, self.coordinates)
        self.pool[key] = cost, _copy(pattern)
        if len(self.pool) > 128:
            retained = sorted(self.pool.items(), key=lambda item: (item[1][0], item[0]))[:64]
            self.pool = dict(retained)

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
                | set(sorted(tails, key=lambda t: (tails[t], t))[:4])
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
        pattern = _copy(seed)
        self.remember(pattern, self.cost(pattern))
        for _ in range(sweeps):
            changes = [self.coordinate(pattern, rid, c) for rid, c in self.coordinates]
            pace.rest()
            if not any(changes):
                break
        return pattern

    def seeds(self):
        centre, quiet = _copy(self.start), _copy(self.start)
        for rid, c in self.coordinates:
            domain = self.problem.radars[rid].domains[c]
            centre[rid][c] = preferred(domain, self.start[rid][c], self.stats[rid, c])
            quiet[rid][c] = domain[-1]
        return self.start, centre, quiet

    def finalists(self, seeds, count):
        ordered = sorted(
            self.pool.values(), key=lambda item: (item[0], _key(item[1], self.coordinates))
        )
        base = [self.start, *seeds, *(pat for _, pat in ordered[:count])]
        variants = self._variants(ordered[0][1]) if ordered else []
        return _unique([self.repair(pat) for pat in (*base, *variants)], self.coordinates)

    def repair(self, pattern):
        result = _copy(pattern)
        for rid, c in self.coordinates:
            stat = self.stats[rid, c]
            current, value = self.start[rid][c], result[rid][c]
            if stat["gap"] is None and not _changes_crossing(stat, current, value):
                result[rid][c] = current
        return result

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


def search(problem, training, *, attenuate=False, sweeps=SWEEPS, shortlist=SHORTLIST):
    engine = _Search(problem, training, attenuate)
    seeds = engine.seeds()
    finals = [engine.run(seed, sweeps) for seed in seeds]
    return engine.finalists((*seeds[1:], *finals), shortlist)

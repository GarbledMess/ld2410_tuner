"""Per-episode decision sufficient statistics over bounded on-disk groups."""

from bisect import bisect_left
from collections import OrderedDict
from dataclasses import replace
from itertools import accumulate

from ...history import pace
from .constants import CELL_SECONDS
from .decision import Decision
from .scoring import cost_from_statistics
from .search_columns import column_block
from .search_data import episode_groups


def _episode(decision, rows, candidates):
    delta = [0.0] * (len(candidates) + 1)
    weighted_delta = [0.0] * (len(candidates) + 1)
    seconds = weighted = 0.0
    for state, source, _parent, confidence, vector, count in rows:
        mass = count * CELL_SECONDS
        seconds += mass
        weighted += mass * confidence
        crossing = decision.crossing(vector, state, source)
        stop = bisect_left(candidates, crossing)
        delta[0] += mass
        delta[stop] -= mass
        weighted_delta[0] += mass * confidence
        weighted_delta[stop] -= mass * confidence
    return seconds, weighted, tuple(accumulate(delta))[:-1], tuple(accumulate(weighted_delta))[:-1]


def _settle(statistics, key, seconds, weighted, seen, weighted_seen):
    confidence = weighted / seconds
    state, source, _parent = key
    source = "human" if source == "human" else "automatic"
    present = state == "present"
    for index, detected in enumerate(seen):
        stats = statistics[index].setdefault(state + "/" + source, [0.0] * 4)
        stats[0] += weighted
        stats[1] += weighted - weighted_seen[index] if present else weighted_seen[index]
        stats[2] += confidence
        stats[3] += confidence * (detected < seconds / 2 if present else detected > 0)


def coordinate_costs(
    problem, store, pattern, rid, channel, candidates, *, folded=True, attenuate=False
):
    """Fold constant outcomes once; deduplicate contested vectors per episode.

    A coordinate's outcome family is nested. Its energy puts a group into one
    prefix of sorted candidates, so two difference-array additions replace a
    candidate loop. Parent/source/confidence strata retain exact multiplicity.
    ``folded=False`` is an unfolded oracle using every original pure cell.
    """
    if any(
        any(r.timing.get(k, 0) for k in ("timeout", "off_delay")) for r in problem.radars.values()
    ):
        from .search_timing import timed_coordinate_costs

        return timed_coordinate_costs(
            problem, store, pattern, rid, channel, candidates, attenuate=attenuate
        )
    order = tuple(sorted(candidates))
    costs = [0.0] * len(order)
    for zone in sorted(problem.zones, key=lambda z: z.group_id):
        from .search_masks import packed_costs

        packed = (
            packed_costs(problem, store, zone, pattern, rid, channel, order, attenuate)
            if folded
            else None
        )
        if packed is not None:
            costs = [v + packed[t] for t, v in zip(order, costs, strict=True)]
            continue
        settled = _settled(problem, store, zone, folded, attenuate)
        statistics = [{lane: list(v) for lane, v in settled.items()} for _ in order]
        episodes = _episode_statistics(
            problem, store, zone, (pattern, rid, channel), order, folded, attenuate
        )
        for key, *sufficient in episodes:
            _settle(statistics, key, *sufficient)
        policy = zone_policy(problem, zone)
        for i, stats in enumerate(statistics):
            costs[i] += linear_cost(store, zone.group_id, stats, policy)
    return {t: round(cost, 9) for t, cost in zip(order, costs, strict=True)}


def _settled(problem, store, zone, folded, attenuate):
    """Unpriced lanes' fixed statistics, which their episodes need not repeat."""
    columns = column_block(problem, store, zone, attenuate) if folded else None
    return {} if columns is None else columns.settled


def _episode_statistics(problem, store, zone, coordinate, order, folded, attenuate):
    """Per parent episode sufficient statistics: columnar when the zone fits."""
    pattern, rid, channel = coordinate
    columns = column_block(problem, store, zone, attenuate) if folded else None
    if columns is not None:
        yield from columns.episodes(pattern, rid, channel, order)
        return
    decision = Decision(problem, store, zone, pattern, rid, channel, attenuate)
    for key, rows in episode_groups(store, zone.group_id, folded=folded):
        pace.rest()
        yield (key, *_episode(decision, rows, order))


def zone_policy(problem, zone):
    quality = problem.policy.quality
    support = quality.get("zones", {}).get(zone.group_id, quality)
    return replace(problem.policy, quality=support)


def conditional_costs(problem, store, pattern, rid, channel, domain, *, attenuate=False):
    """Accumulate global conditional lanes, then evaluate strict tails in O(101)."""
    result = dict.fromkeys(domain, 0.0)
    for zone in problem.zones:
        from .search_masks import packed_costs

        packed = packed_costs(
            problem, store, zone, pattern, rid, channel, domain, attenuate, episodes=False
        )
        if packed is not None:
            result = {t: result[t] + packed[t] for t in domain}
            continue
        columns = column_block(problem, store, zone, attenuate)
        if columns is not None:
            lanes = columns.lanes(pattern, rid, channel)
            _tail_costs(result, lanes, zone_policy(problem, zone), store, zone.group_id)
            continue
        lanes = {}
        decision = Decision(problem, store, zone, pattern, rid, channel, attenuate)
        rows = store._database.execute(
            "SELECT state,source,confidence,vector,n FROM search_groups WHERE zone=?",
            (zone.group_id,),
        )
        for state, source, confidence, vector, count in rows:
            key = state + "/" + ("human" if source == "human" else "automatic")
            total, bins = lanes.setdefault(key, [0.0, [0.0] * 103])
            mass = confidence * count * CELL_SECONDS
            lanes[key][0] = total + mass
            crossing = decision.crossing(vector, state, source)
            bins[crossing + 1] += mass
        _tail_costs(result, lanes, zone_policy(problem, zone), store, zone.group_id)
    return result


def _tail_costs(result, lanes, policy, store, zone):
    tails = {key: tuple(accumulate(reversed(bins))) for key, (_total, bins) in lanes.items()}
    for threshold in result:
        statistics = {}
        for key, (total, _bins) in lanes.items():
            detected = tails[key][100 - threshold]
            error = total - detected if key.startswith("present/") else detected
            statistics[key] = [total, error, 0.0, 0.0]
        result[threshold] += linear_cost(store, zone, statistics, policy)


def linear_cost(store, zone, statistics, policy):
    """Compile source shares once; errors are affine for fixed evidence masses."""
    if not hasattr(store, "_cost_coefficients"):
        store._cost_coefficients = OrderedDict()
    key = (
        zone,
        policy.automatic_weight,
        policy.reference,
        id(policy.quality),
        tuple((k, round(v[0], 9), round(v[2], 9)) for k, v in sorted(statistics.items())),
    )
    cache = store._cost_coefficients
    if key not in cache:
        cache[key] = _coefficients(statistics, policy)
        while len(cache) > 256:
            cache.popitem(last=False)
    coefficients = cache[key]
    return sum(statistics[k][i] * value for k, i, value in coefficients)


def _coefficients(statistics, policy):
    base = {key: [v[0], 0.0, v[2], 0.0] for key, v in statistics.items()}
    result = []
    for key in sorted(base):
        for index in (1, 3):
            if base[key][index - 1]:
                base[key][index] = 1.0
                value = cost_from_statistics(base, policy)["partial_cost"]
                base[key][index] = 0.0
                result.append((key, index, value))
    return tuple(result)

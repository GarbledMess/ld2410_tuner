"""Training-only selection: quiet-presence prior, per-zone guards and evidence band.

Selection cost per zone is its base training cost plus
``min(QUIET_CAP, quiet_weight(occupied) * quiet_cost)``; linked zones sum costs
and evidence bands while sharing one radar vector. A candidate may not mostly miss
more occupied episodes, nor be mostly false-active in more empty periods, than the
start in any zone (observed episode counts, never weighted fractions). The start
is kept unless the best allowed selection cost beats it by more than the band;
within the band the most central gap margin wins, then lower thresholds, then
cost and candidate order. The quiet prior never enters a reported error cost.

A zone's presence priority (its metrics' ``missed_weight`` over ``MISSED_WEIGHT``) scales
its quiet prior, its presence price and its presence floor alike; at 1 they are unchanged.
Above 1 the zone drops its false-active guard, below 1 its missed-episode guard.
"""

from .constants import (
    FALSE_TRIGGER_SECONDS,
    MISSED_WEIGHT,
    PRESENCE_FLOOR,
    PRESENCE_PRICE,
    QUIET_CAP,
    QUIET_SECONDS,
    QUIET_SLACK,
    ROBUST_WEIGHT,
)
from .domains import evidence_margin
from .evidence import _labels

GUARDS = ("mostly_missed_episodes", "empty_periods_mostly_false_active")


def quiet_weight(occupied_seconds: float) -> float:
    """Prior weight: full below ``QUIET_SECONDS`` of occupied evidence, then 1/t."""
    return ROBUST_WEIGHT * min(1.0, QUIET_SECONDS / max(1.0, occupied_seconds))


def priority(metrics: dict) -> float:
    """A zone's presence priority: its missed weight over the default one."""
    return metrics.get("missed_weight", MISSED_WEIGHT) / MISSED_WEIGHT


def _attenuated(metrics, term, kind):
    """``term`` as if missed presence weighed ``MISSED_WEIGHT``, divided by that weight."""
    if metrics.get("missed_weight", MISSED_WEIGHT) == MISSED_WEIGHT:
        return metrics[term] / MISSED_WEIGHT
    parts = metrics["class_components"]
    return parts["present"][kind] + parts["not_present"][kind] / MISSED_WEIGHT


def quiet_cost(metrics: dict) -> float:
    """500 x (attenuated missed fraction beyond the slack + mostly missed fraction),
    times the zone's presence priority."""
    missed = _attenuated(metrics, "time_term", "time_fraction")
    mostly = _attenuated(metrics, "episode_term", "episode_fraction")
    cost = 500 * (max(0.0, missed - QUIET_SLACK) + mostly)
    factor = priority(metrics)
    return cost if factor == 1 else factor * cost


def occupied_seconds(problem, zone):
    """Labelled occupied time of one zone inside the fit window (before coverage)."""
    total = 0.0
    for lo, hi, span in _labels(zone.truth):
        if span.state == "present":
            total += max(0.0, min(hi, problem.end) - max(lo, problem.start))
    return total


def weights(problem):
    return {z.group_id: quiet_weight(occupied_seconds(problem, z)) for z in problem.zones}


def base_cost(metrics):
    return metrics["cost"] if metrics["cost"] is not None else metrics["partial_cost"]


def _zones(score):
    return score["zones"] if "zones" in score else {None: score}


def selection_costs(problem, scores, quiet):
    """Per candidate: (selection cost, raw quiet cost), summed over zones."""
    w = weights(problem)
    if len(w) == 1:
        w[None] = next(iter(w.values()))
    out = []
    for i, score in enumerate(scores):
        quiet_zones = _zones(quiet[i]) if quiet is not None else {}
        selection = raw = 0.0
        for gid, metrics in _zones(score).items():
            q = quiet_cost(quiet_zones[gid]) if gid in quiet_zones else 0.0
            selection += base_cost(metrics) + min(QUIET_CAP, w.get(gid, ROBUST_WEIGHT) * q)
            raw += q
        out.append((selection, raw))
    return out


def _guards(metrics):
    """The guards a zone keeps: one that favours presence accepts more mostly false-active
    empty periods, one that favours empty time more mostly missed episodes."""
    factor = priority(metrics)
    if factor > 1:
        return ("mostly_missed_episodes",)
    if factor < 1:
        return ("empty_periods_mostly_false_active",)
    return GUARDS


def _allowed(scores):
    start = {
        gid: metrics.get("guard_counts", metrics) for gid, metrics in _zones(scores[0]).items()
    }
    return [
        i
        for i, score in enumerate(scores)
        if all(
            metrics.get("guard_counts", metrics)[g] <= start[gid][g]
            for gid, metrics in _zones(score).items()
            for g in _guards(metrics)
        )
    ]


def _presence_price(score):
    """Priced missed human presence plus false time and triggers; None without human presence."""
    total, found = 0.0, False
    for metrics in _zones(score).values():
        values = metrics["sufficient_statistics"].get("present/human")
        if values and values[0]:
            found = True
            total += PRESENCE_PRICE * priority(metrics) * values[1]
        total += metrics["false_active_seconds"]
        total += FALSE_TRIGGER_SECONDS * metrics["false_triggers"]
    return total if found else None


def presence_shift(scores):
    """Index of the cheapest lowering of ``scores[0]``; ties keep the higher thresholds."""
    prices = [_presence_price(score) for score in scores]
    if prices[0] is None:
        return 0
    return min(_allowed(scores), key=lambda i: (prices[i], i))


def _human_recall(metrics):
    """Human-labelled recall, its shortfall scaled by the zone's presence priority, so the
    floor allows ``PRESENCE_FLOOR / priority`` less recall than the best candidate."""
    # Automatic guesses are weaker evidence: they may spend margin, never force a threshold.
    values = metrics.get("sufficient_statistics", {}).get("present/human")
    if not values or not values[0]:
        return None
    factor = priority(metrics)
    return 1 - values[1] / values[0] if factor == 1 else 1 - factor * values[1] / values[0]


def _recall(score):
    values = (_human_recall(m) for m in _zones(score).values())
    return min((v for v in values if v is not None), default=0.0)


def _presence_floor(scores, allowed):
    """Catching presence outranks cost: keep the start, drop candidates far below the best."""
    floor = max(_recall(scores[i]) for i in allowed) - PRESENCE_FLOOR
    return [i for i in allowed if i == 0 or _recall(scores[i]) >= floor]


def _margin(scores):
    return sum(
        evidence_margin(m["occupied_episodes"], m["empty_periods"])
        for m in _zones(scores[0]).values()
    )


def gap_margin(statistics, rid, channel, threshold):
    gap = (statistics.get((rid, channel)) or {}).get("gap")
    return None if gap is None else min(threshold - gap[0], gap[1] - threshold)


def _ranker(problem, patterns, tied, costs, statistics):
    varying = [
        (rid, c)
        for rid, radar in problem.radars.items()
        for c in radar.channels
        if len({patterns[i][rid][c] for i in tied}) > 1
        and (statistics.get((rid, c)) or {}).get("gap") is not None
    ]

    def rank(i):
        margin = min(
            (gap_margin(statistics, rid, c, patterns[i][rid][c]) for rid, c in varying), default=0
        )
        total = sum(sum(patterns[i][rid].values()) for rid in problem.radars)
        return -margin, total, round(costs[i], 9), i

    return rank, varying


def choose(problem, patterns, scores, quiet, statistics):
    """Index of the chosen score (0 is the start) and the selection audit."""
    costs = [c for c, _ in selection_costs(problem, scores, quiet)]
    guarded = _allowed(scores)
    allowed = _presence_floor(scores, guarded)
    margin = _margin(scores)
    best = min(costs[i] for i in allowed)
    info = {
        "margin": round(margin, 6),
        "start_cost": costs[0],
        "best_cost": best,
        "guarded": len(scores) - len(guarded),
        "presence_floored": len(guarded) - len(allowed),
        "selection_costs": costs,
    }
    if costs[0] - best <= margin:
        info["why"] = "improvement within evidence margin"
        return 0, info
    tied = [i for i in allowed if i and costs[i] <= best + margin]
    rank, varying = _ranker(problem, patterns, tied, costs, statistics)
    info.update(why="fitted", tied=len(tied), margin_gates=len(varying))
    return min(tied, key=rank), info

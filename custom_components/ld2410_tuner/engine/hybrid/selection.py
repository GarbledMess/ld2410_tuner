"""Training-only selection: quiet-presence prior, per-zone guards and evidence band.

Selection cost per zone is its base training cost plus
``min(QUIET_CAP, quiet_weight(occupied) * quiet_cost)``; linked zones sum costs
and evidence bands while sharing one radar vector. A candidate may not mostly miss
more occupied episodes, nor be mostly false-active in more empty periods, than the
start in any zone (observed episode counts, never weighted fractions). The start
is kept unless the best allowed selection cost beats it by more than the band;
within the band the most central gap margin wins, then lower thresholds, then
cost and candidate order. The quiet prior never enters a reported error cost.
"""

from .constants import MISSED_WEIGHT, QUIET_CAP, QUIET_SECONDS, QUIET_SLACK, ROBUST_WEIGHT
from .domains import evidence_margin
from .evidence import _labels

GUARDS = ("mostly_missed_episodes", "empty_periods_mostly_false_active")


def quiet_weight(occupied_seconds: float) -> float:
    """Prior weight: full below ``QUIET_SECONDS`` of occupied evidence, then 1/t."""
    return ROBUST_WEIGHT * min(1.0, QUIET_SECONDS / max(1.0, occupied_seconds))


def quiet_cost(metrics: dict) -> float:
    """500 x (attenuated missed fraction beyond the slack + mostly missed fraction)."""
    missed = metrics["time_term"] / MISSED_WEIGHT
    mostly = metrics["episode_term"] / MISSED_WEIGHT
    return 500 * (max(0.0, missed - QUIET_SLACK) + mostly)


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


def _allowed(scores):
    start = _zones(scores[0])
    return [
        i
        for i, score in enumerate(scores)
        if all(
            metrics[g] <= start[gid][g] for gid, metrics in _zones(score).items() for g in GUARDS
        )
    ]


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
    allowed = _allowed(scores)
    margin = _margin(scores)
    best = min(costs[i] for i in allowed)
    info = {
        "margin": round(margin, 6),
        "start_cost": costs[0],
        "best_cost": best,
        "guarded": len(scores) - len(allowed),
        "selection_costs": costs,
    }
    if costs[0] - best <= margin:
        info["why"] = "improvement within evidence margin"
        return 0, info
    tied = [i for i in allowed if i and costs[i] <= best + margin]
    rank, varying = _ranker(problem, patterns, tied, costs, statistics)
    info.update(why="fitted", tied=len(tied), margin_gates=len(varying))
    return min(tied, key=rank), info

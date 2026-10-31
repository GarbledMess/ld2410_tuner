"""Legal crossing candidates and duration-based interior-gap preferences."""

from bisect import bisect_left

from .constants import TOP_CROSSINGS


def quantile(histogram, fraction):
    mass = sum(histogram)
    if not mass:
        return None
    target = fraction * mass
    total = 0
    for value, count in enumerate(histogram):
        total += count
        if total >= target:
            return value
    return len(histogram) - 1


def legal_up(domain, value):
    return domain[min(len(domain) - 1, bisect_left(domain, value))]


def legal_down(domain, value):
    return max((t for t in domain if t <= value), default=domain[0])


def gap_values(domain, gap):
    if gap is None:
        return ()
    lo, hi = gap
    inside = [t for t in domain if lo < t < hi]
    if not inside:
        return ()
    margin = max(min(t - lo, hi - t) for t in inside)
    return tuple(t for t in inside if min(t - lo, hi - t) == margin)


def preferred(domain, current, statistics):
    centres = gap_values(domain, statistics.get("gap"))
    if centres:
        return centres[0]
    ceiling = statistics.get("ceiling")
    if ceiling is None or not any(statistics["hp"][ceiling + 1 :]):
        return current
    return legal_up(domain, ceiling)


def candidate_values(domain, current, statistics, costs):
    """Keep current/max, supported crossings, central gaps, and four tail minima."""
    out = {current, domain[-1]}
    hp, he = statistics.get("hp", ()), statistics.get("he", ())
    for q in (0.99, 0.999, 1):
        value = quantile(he, q)
        if value is not None:
            out.add(legal_up(domain, value))
    for q in (0.05, 0.25, 0.5):
        value = quantile(hp, q)
        if value is not None:
            out.add(legal_down(domain, value - 1))
    gap = statistics.get("gap")
    if gap:
        out.update((legal_up(domain, gap[0]), legal_down(domain, gap[1] - 1)))
        out.update(gap_values(domain, gap))
    out.update(sorted(costs, key=lambda t: (costs[t], t))[:TOP_CROSSINGS])
    return tuple(sorted(out))


def channel_statistics(hp, he):
    ceiling = max((v for v, count in enumerate(he) if count), default=None)
    floor = quantile(hp, 0.25)
    gap = (
        (ceiling, floor) if ceiling is not None and floor is not None and ceiling < floor else None
    )
    return {"hp": hp, "he": he, "ceiling": ceiling, "q25": floor, "gap": gap}

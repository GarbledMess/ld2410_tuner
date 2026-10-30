"""Exact selected-channel physical replay and design time/episode scoring.

All thresholds are integer: ceil quantisation preserves raw crossings exactly.
Unknown coverage is excluded, reported separately, and resets timing.
"""

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from math import isfinite


def union(items):
    out = []
    for a, b in sorted(items):
        if b <= a:
            continue
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(b, out[-1][1]))
        else:
            out.append((a, b))
    return out


def intersect(left, right):
    out = []
    i = j = 0
    while i < len(left) and j < len(right):
        a, b = left[i]
        c, d = right[j]
        if min(b, d) > max(a, c):
            out.append((max(a, c), min(b, d)))
        if b < d:
            i += 1
        else:
            j += 1
    return out


def duration(items):
    return sum(b - a for a, b in items)


def subtract(base, cut):
    out = []
    j = 0
    for a, b in base:
        while j < len(cut) and cut[j][1] <= a:
            j += 1
        k = j
        at = a
        while k < len(cut) and cut[k][0] < b:
            c, d = cut[k]
            if c > at:
                out.append((at, min(c, b)))
            at = max(at, d)
            if at >= b:
                break
            k += 1
        if at < b:
            out.append((at, b))
    return out


def _append(out, a, b):
    if b <= a:
        return
    if out and a <= out[-1][1]:
        out[-1] = (out[-1][0], max(b, out[-1][1]))
    else:
        out.append((a, b))


@dataclass(frozen=True)
class Replay:
    coverage: tuple
    active: tuple


def replay(radar, thresholds, window):
    a, b = window
    if not isfinite(a) or not isfinite(b) or b < a:
        raise ValueError("Invalid window")
    for i, t in thresholds.items():
        if (
            not isinstance(i, int)
            or not 0 <= i < 18
            or not isfinite(t)
            or int(t) != t
            or not 0 <= t <= 100
        ):
            raise ValueError("Integer channels and integer thresholds 0..100 required")
    cfg = radar.timing or {}
    hold = cfg.get("timeout")
    on = cfg.get("on_delay")
    off = cfg.get("off_delay")
    if hold is None:
        hold = on = off = 0
    elif on is None or off is None:
        on = off = 0
    if any(not isfinite(v) or v < 0 for v in (hold, on, off)):
        raise ValueError("Invalid timing")
    cov = []
    active = []
    hits = []
    low = high = None
    columns = [(radar.columns[i], t) for i, t in thresholds.items()]

    def close():
        if low is None:
            return
        _append(cov, low, high)
        held = []
        for x, y in hits:
            _append(held, x, min(high, y + hold))
        begin = expires = None
        for x, y in held:
            if begin is not None and x <= expires:
                expires = min(high, y + off)
                continue
            if begin is not None:
                _append(active, begin, expires)
            begin = x + on if y - x >= on else None
            expires = min(high, y + off)
        if begin is not None:
            _append(active, begin, expires)

    previous = float("-inf")
    for j, (t, e) in enumerate(zip(radar.times, radar.ends, strict=False)):
        if not isfinite(t) or not isfinite(e) or t < previous or e < t:
            raise ValueError("Invalid physical rows")
        previous = e
        if e == t:
            continue
        usable = all(col[j] != 255 for col, _ in columns)
        if not usable or (high is not None and t != high):
            close()
            low = high = None
            hits = []
        if not usable:
            continue
        if low is None:
            low = t
        high = e
        if any(col[j] > threshold for col, threshold in columns):
            _append(hits, t, e)
    close()

    def clip(items):
        return tuple(
            (max(a, x), min(b, y)) for x, y in items if y > a and x < b and min(b, y) > max(a, x)
        )

    return Replay(clip(cov), clip(active))


def _episodes(truth, window):
    a, b = window
    out = []
    for x, y, state, src, confidence in truth:
        x, y = max(x, a), min(y, b)
        if y <= x or state not in ("present", "not_present"):
            continue
        if out and x == out[-1][1] and (state, src) == out[-1][2:4]:
            first, last, _, _, old_conf = out[-1]
            average = (old_conf * (last - first) + confidence * (y - x)) / (y - first)
            out[-1] = (first, y, state, src, average)
        else:
            out.append((x, y, state, src, confidence))
    return out


def margins(radar, thresholds, truth, window):
    result = {}
    for gate, t in thresholds.items():
        per_source = {
            (state, source): [0.0] * 101
            for state in ("present", "not_present")
            for source in ("human", "automatic")
        }
        for a, b, state, src, confidence in truth:
            a, b = max(a, window[0]), min(b, window[1])
            if b <= a or state not in ("present", "not_present"):
                continue
            bins = per_source[(state, "human" if src == "human" else "automatic")]
            lo, hi = bisect_right(radar.ends, a), bisect_left(radar.times, b)
            for j in range(lo, hi):
                v = radar.columns[gate][j]
                if v != 255:
                    bins[v] += max(0, min(b, radar.ends[j]) - max(a, radar.times[j])) * confidence
        hist = {
            state: per_source[(state, "human")]
            if sum(per_source[(state, "human")])
            else per_source[(state, "automatic")]
            for state in ("present", "not_present")
        }
        empty = next((i for i in range(100, -1, -1) if hist["not_present"][i] > 0), None)
        total = sum(hist["present"])
        cumulative = 0
        floor = None
        if total:
            for i, mass in enumerate(hist["present"]):
                cumulative += mass
                if cumulative >= total * 0.25:
                    floor = i
                    break
        result[str(gate)] = {
            "empty_ceiling": empty,
            "occupied_floor_q25": floor,
            "margin": min(t - empty, floor - t)
            if empty is not None and floor is not None
            else None,
        }
    return result


def referee_score(
    radars, thresholds, truth, window, weights=None, include_margins=True, replayed=None
):
    weights = weights or {"missed": 5, "automatic": 0, "auto_class_cap": 0.25}
    replays = (
        list(replayed.values())
        if replayed is not None
        else [
            replay(
                r,
                thresholds[k],
                (
                    min(window[0], r.times[0]) if r.times else window[0],
                    max(window[1], r.ends[-1]) if r.ends else window[1],
                ),
            )
            for k, r in radars.items()
        ]
    )
    coverage = [window]
    for r in replays:
        coverage = intersect(coverage, list(r.coverage))
    active = union(x for r in replays for x in r.active)
    starts = [a for a, b in active]
    totals = dict.fromkeys(("present", "not_present"), 0.0)
    detected = dict(totals)
    eps = dict.fromkeys(totals, 0)
    bad = dict(eps)
    false_triggers = 0
    triggered = mostly_active = 0
    uncovered = 0
    by_source = {}
    occupied = []
    previous = float("-inf")
    for a, b, state, src, c in truth:
        if not all(isfinite(v) for v in (a, b, c)) or b < a or a < previous or not 0 <= c <= 1:
            raise ValueError("Truth must be ordered, disjoint, finite, with confidence 0..1")
        previous = b
        a, b = max(a, window[0]), min(b, window[1])
        if b <= a or state not in totals:
            continue
        observed = intersect([(a, b)], coverage)
        mass = duration(observed)
        uncovered += (b - a) - mass
        if mass <= 0:
            continue
        seen = duration(intersect(observed, active))
        totals[state] += mass
        detected[state] += seen
        if state == "present":
            occupied.extend(observed)
        item = by_source.setdefault(
            (state, "human" if src == "human" else "automatic"), [0.0, 0.0, 0.0, 0.0]
        )
        item[0] += mass * c
        item[1] += (mass - seen if state == "present" else seen) * c
    for a, b, state, src, c in _episodes(truth, window):
        observed = intersect([(a, b)], coverage)
        mass = duration(observed)
        if mass <= 0:
            continue
        seen = duration(intersect(observed, active))
        trigger_times = starts[bisect_left(starts, a) : bisect_left(starts, b)]
        n = sum(any(x <= t < y for x, y in observed) for t in trigger_times)
        # Occupied: mostly missed (detected < half). Empty: a false trigger starts in it
        # OR it is false-active for more than half its duration (the mirror rule; an
        # always-on pattern never "starts" a trigger inside the period).
        if state == "present":
            error = float(seen < mass * 0.5)
        else:
            triggered += n > 0
            mostly_active += seen > mass * 0.5
            error = float(n > 0 or seen > mass * 0.5)
        eps[state] += 1
        bad[state] += int(error)
        item = by_source.setdefault(
            (state, "human" if src == "human" else "automatic"), [0.0, 0.0, 0.0, 0.0]
        )
        item[2] += c
        item[3] += error * c
        if state == "not_present":
            false_triggers += n
    longest = max((y - x for x, y in subtract(union(occupied), active)), default=0)
    time = episode = 0.0
    components = {}
    for state in totals:
        h = by_source.get((state, "human"), [0, 0, 0, 0])
        auto = by_source.get((state, "automatic"), [0, 0, 0, 0])

        def rates(v):
            return (v[1] / v[0] if v[0] else 0, v[3] / v[2] if v[2] else 0)

        ht, he = rates(h)
        at, ae = rates(auto)
        if h[0]:
            share = (
                weights.get("automatic", 0)
                * weights.get("auto_class_cap", 0.25)
                * min(1, auto[0] / h[0])
            )
            eshare = (
                weights.get("automatic", 0)
                * weights.get("auto_class_cap", 0.25)
                * (
                    auto[2] / (h[2] + auto[2])
                    if weights.get("adaptive", False)
                    else min(1, auto[2] / h[2])
                )
            )
            ct, ce = ht + share * at, he + eshare * ae
        else:
            ct, ce = at, ae
        factor = weights.get("missed", 5) if state == "present" else 1
        time += factor * ct
        episode += factor * ce
        components[state] = {"time_fraction": ct, "episode_fraction": ce}
    complete = all(
        sum(by_source.get((state, source), [0])[0] for source in ("human", "automatic")) > 0
        for state in totals
    )
    out = {
        "sufficient_statistics": {
            state + "/" + source: values for (state, source), values in by_source.items()
        },
        "cost": 100 * (time + episode) if complete else None,
        "time_term": time,
        "episode_term": episode,
        "scorable_both_classes": complete,
        "occupied_recall": detected["present"] / totals["present"] if totals["present"] else None,
        "occupied_seconds": totals["present"],
        "empty_seconds": totals["not_present"],
        "occupied_episodes": eps["present"],
        "empty_periods": eps["not_present"],
        "mostly_missed_episodes": bad["present"],
        "empty_periods_with_false_trigger": triggered,
        "empty_periods_mostly_false_active": mostly_active,
        "empty_periods_failed": bad["not_present"],
        "longest_miss_seconds": longest,
        "false_active_seconds": detected["not_present"],
        "false_triggers": false_triggers,
        "uncovered_label_seconds": uncovered,
        "covered_seconds": duration(coverage),
        "class_components": components,
        "weighting": "human primary; automatic confidence, class cap and episode cap; missing class automatic fallback",
    }
    if include_margins:
        out["margins"] = {k: margins(r, thresholds[k], truth, window) for k, r in radars.items()}
    return out

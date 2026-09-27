"""Locate recorded periods contributing most to the current recommendation's errors."""

from .duration import DurationGroup
from .timing import _sessions


def period_influence(groups, thresholds, config=None):
    periods = []
    for label, rows in groups.items():
        barriers = [row[0] for other, group in groups.items() if other != label for row in group]
        periods.extend(_periods(rows, sorted(barriers), label, thresholds, config))
    totals = {
        label: sum(p["error_seconds"] for p in periods if p["state"] == label) for label in groups
    }
    empty_time = sum(p["observed_seconds"] for p in periods if p["state"] == "not_present")
    for period in periods:
        period["error_share_percent"] = (
            100 * period["error_seconds"] / totals[period["state"]]
            if totals[period["state"]]
            else 0.0
        )
        period["score_without_period"] = _score_without(period, totals, empty_time)
    return sorted(
        (p for p in periods if p["error_seconds"] > 0), key=lambda p: -p["error_seconds"]
    )[:6]


def _periods(rows, barriers, label, thresholds, config):
    periods = []
    for _offset, part in _sessions(rows, barriers):
        bits = sum(
            1 << i
            for i, row in enumerate(part)
            if any(row[1].get(key, -1) > value for key, value in thresholds.items())
        )
        m = DurationGroup(part, config, absent=label == "not_present").measure(bits)
        error = (
            m["active_seconds"]
            if label == "not_present"
            else m["observed_seconds"] - m["active_seconds"]
        )
        periods.append(
            {
                "start": part[0][0],
                "end": part[-1][0],
                "state": label,
                "samples": len(part),
                "observed_seconds": m["observed_seconds"],
                "error_seconds": max(0.0, error),
                "comparison": "same_thresholds_not_refitted",
                "excluded_automatically": False,
            }
        )
    return periods


def _score_without(period, totals, empty_time):
    remaining = empty_time - period["observed_seconds"]
    if period["state"] != "not_present" or remaining <= 0:
        return None
    return -100 * max(0.0, totals["not_present"] - period["error_seconds"]) / remaining

"""Reviewable signal conflicts without inventing occupancy truth or deleting samples."""

from .constants import SAMPLE_SECONDS
from .influence import period_influence
from .timing_metrics import observations


def review_evidence(groups, thresholds, timing=None):
    sessions, periods = [], []
    selected = observations(groups, thresholds, timing)
    for label in groups:
        rows = [row for row, _hit in selected if row[2] == label]
        errors = [hit != (label == "present") for row, hit in selected if row[2] == label]
        sessions.extend(_sessions(rows, errors, label))
        periods.extend(_periods(rows, errors, label, thresholds))
    periods.sort(key=lambda p: (-p["samples"], p["start"]))
    return {
        "sessions": sorted(sessions, key=lambda s: (-s["errors"], s["start"]))[:24],
        "periods": _review_periods(periods),
        "period_count": len(periods),
        "influence": period_influence(groups, thresholds, timing),
        "excluded_automatically": False,
    }


def _sessions(rows, errors, label):
    sessions = []
    for row, error in zip(rows, errors, strict=True):
        timestamp = row[0]
        if not sessions or timestamp - sessions[-1]["end"] > SAMPLE_SECONDS:
            sessions.append(
                {"start": timestamp, "end": timestamp, "state": label, "samples": 0, "errors": 0}
            )
        current = sessions[-1]
        current["end"] = timestamp + SAMPLE_SECONDS
        current["samples"] += 1
        current["errors"] += error
    return sessions


def _periods(rows, errors, label, thresholds):
    runs, active = [], []
    for index, error in enumerate(errors):
        if active and (not error or rows[index][0] - rows[active[-1]][0] > SAMPLE_SECONDS * 2):
            runs.append(active)
            active = []
        if error:
            active.append(index)
    if active:
        runs.append(active)
    return [_describe(rows, errors, run, label, thresholds) for run in runs]


def _describe(rows, errors, run, label, thresholds):
    first, last = run[0], run[-1]
    gates = _triggering_gates(rows, run, label, thresholds)
    bounded = first > 0 and last + 1 < len(rows)
    isolated = bounded and len(run) <= 6 and not errors[first - 1] and not errors[last + 1]
    if isolated:
        isolated = (
            rows[first][0] - rows[first - 1][0] <= 12 and rows[last + 1][0] - rows[last][0] <= 12
        )
    return {
        "start": rows[first][0],
        "end": rows[last][0] + SAMPLE_SECONDS,
        "state": label,
        "samples": len(run),
        "observed_span_seconds": rows[last][0] - rows[first][0],
        "duration_known": False,
        "short_burst": bool(isolated),
        "gates": gates,
    }


def _triggering_gates(rows, run, label, thresholds):
    gates = {}
    for index in run:
        for key, threshold in thresholds.items():
            value = rows[index][1].get(key, -1)
            if label == "not_present" and value > threshold:
                gates[key] = max(gates.get(key, 0), value)
    return gates


def _review_periods(periods):
    # Reserve room for each class: numerous empty-room bursts must not hide
    # the quiet-presence intervals that keep the thresholds low.
    selected = []
    for state in ("present", "not_present"):
        selected.extend([p for p in periods if p["state"] == state][:12])
    selected.extend([p for p in periods if p not in selected][: 24 - len(selected)])
    return sorted(selected, key=lambda p: (-p["samples"], p["start"]))

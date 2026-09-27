"""Elapsed-time calibration scores over observed sessions, never a live detector."""

from bisect import bisect_right
from functools import lru_cache

from .constants import MISSED_TIME_COST
from .timing import _sessions, _Window, bit_runs


def _clip(intervals, start, end):
    return [(max(a, start), min(b, end)) for a, b in intervals if b > start and a < end]


def _raw_intervals(window, bits):
    times = window.times
    return [
        (
            (times[first - 1] + times[first]) / 2 if first else times[0],
            (times[last] + times[last + 1]) / 2 if last + 1 < len(times) else times[-1],
        )
        for first, last in bit_runs(bits)
    ]


def _recent_start(windows):
    spans = [(w.times[0] + max(w.hold + w.off, w.on), w.times[-1]) for _, w, _ in windows]
    spans = [(a, b) for a, b in spans if b > a]
    remaining = sum(b - a for a, b in spans) * 0.8
    for start, end in spans:
        if remaining <= end - start:
            return start + remaining
        remaining -= end - start
    return 0


class _ConfidenceTime:
    """Piecewise confidence over midpoint cells; dense telemetry adds no time."""

    def __init__(self, rows):
        self.times = [rows[0][0]] + [
            (a[0] + b[0]) / 2 for a, b in zip(rows, rows[1:], strict=False)
        ]
        self.weights = [row[3] if len(row) > 3 else 1.0 for row in rows]
        self.prefix = [0.0]
        for i in range(1, len(rows)):
            self.prefix.append(
                self.prefix[-1] + (self.times[i] - self.times[i - 1]) * self.weights[i - 1]
            )

    def at(self, timestamp):
        i = max(0, bisect_right(self.times, timestamp) - 1)
        return self.prefix[i] + (timestamp - self.times[i]) * self.weights[i]

    def between(self, start, end):
        return self.at(end) - self.at(start)


class DurationGroup:
    def __init__(self, rows, config=None, *, absent=False, barriers=()):
        config = config or {}
        self.timed = config.get("timeout") is not None
        self.absent = absent
        hold = config.get("timeout") or 0
        known = (
            self.timed
            and config.get("on_delay") is not None
            and config.get("off_delay") is not None
        )
        on, off = (config["on_delay"], config["off_delay"]) if known else (0, 0)
        self.windows = [
            (offset, _Window(part, hold, on, off), _ConfidenceTime(part))
            for offset, part in _sessions(rows, barriers)
        ]
        self.unscored_mask = sum(
            window.mask << offset
            for offset, window, _ in self.windows
            if window.times[-1] <= window.times[0] + max(window.hold + window.off, window.on)
        )
        self.recent_start = _recent_start(self.windows)
        self.measure = lru_cache(maxsize=256)(self._measure)

    def _intervals(self, window, bits, latest):
        if not self.timed:
            return _raw_intervals(window, bits)
        if self.absent:
            return window._upper(bits)
        return window._lower(bits) if latest else window._possible_onset(bits)

    def _measure(self, bits, recent=False, latest=False):
        result = {
            "observed_seconds": 0.0,
            "active_seconds": 0.0,
            "weighted_seconds": 0.0,
            "weighted_active_seconds": 0.0,
            "episodes": 0,
            "missed_episodes": 0,
            "events": 0,
            "longest_inactive_seconds": 0.0,
            "unscored_samples": 0,
        }
        for offset, window, confidence in self.windows:
            if recent and window.times[-1] < self.recent_start:
                continue
            start = window.times[0] + max(window.hold + window.off, window.on)
            if recent:
                start = max(start, self.recent_start)
            end = window.times[-1]
            if end <= start:
                result["unscored_samples"] += len(window.times)
                continue
            intervals = _clip(
                self._intervals(window, (bits >> offset) & window.mask, latest), start, end
            )
            _add_window(result, start, end, intervals, confidence)
        return result


def _add_window(result, start, end, intervals, confidence):
    result["observed_seconds"] += end - start
    result["active_seconds"] += sum(b - a for a, b in intervals)
    result["weighted_seconds"] += confidence.between(start, end)
    result["weighted_active_seconds"] += sum(confidence.between(a, b) for a, b in intervals)
    result["episodes"] += 1
    result["missed_episodes"] += not intervals
    result["events"] += len(intervals)
    boundaries = [start] + [time for interval in intervals for time in interval] + [end]
    longest = max(
        (b - a for a, b in zip(boundaries[::2], boundaries[1::2], strict=True)), default=0.0
    )
    result["longest_inactive_seconds"] = max(result["longest_inactive_seconds"], longest)


class DurationReplay:
    def __init__(self, present, absent, config=None):
        self.present = DurationGroup(present, config, barriers=[row[0] for row in absent])
        self.absent = DurationGroup(
            absent, config, absent=True, barriers=[row[0] for row in present]
        )

    def summary(self, detected, false, recent=False):
        present = self.present.measure(detected, recent)
        latest = self.present.measure(detected, recent, True)
        absent = self.absent.measure(false, recent)
        ptime, ntime = present["observed_seconds"], absent["observed_seconds"]
        recall = present["active_seconds"] / ptime if ptime else None
        lower = latest["active_seconds"] / ptime if ptime else None
        penalty = -100 * absent["active_seconds"] / ntime if ntime else None
        return {
            "presence_recall": recall,
            "presence_recall_lower": lower,
            "false_positive_percent": -penalty if penalty is not None else None,
            "false_positive_score": penalty,
            "present_seconds": ptime,
            "empty_seconds": ntime,
            "missed_seconds": max(0.0, ptime - present["active_seconds"]),
            "missed_seconds_upper": max(0.0, ptime - latest["active_seconds"]),
            "false_positive_seconds": absent["active_seconds"],
            "presence_episodes": present["episodes"],
            "missed_presence_episodes": present["missed_episodes"],
            "longest_missed_seconds": present["longest_inactive_seconds"],
            "longest_missed_seconds_upper": latest["longest_inactive_seconds"],
            "false_trigger_events": absent["events"],
            "unscored_presence_samples": present["unscored_samples"],
            "unscored_empty_samples": absent["unscored_samples"],
            "basis": "observed_session_time_estimate",
            "error_cost": _error_cost(
                1 - lower if lower is not None else 0.0,
                -penalty / 100 if penalty is not None else 0.0,
            ),
            "missed_time_cost": MISSED_TIME_COST,
        }

    def rank(self, detected, false):
        # A quality target is not a free miss budget, nor an infinite penalty.
        full, recent = (self.summary(detected, false, recent) for recent in (False, True))
        return (
            round(full["error_cost"], 10),
            sum(m["missed_presence_episodes"] for m in (full, recent)),
            round(recent["error_cost"], 10),
            round(full["missed_seconds_upper"], 6),
            round(full["false_positive_percent"] or 0.0, 9),
            full["false_trigger_events"],
            (self.present.unscored_mask & ~detected).bit_count(),
            (self.absent.unscored_mask & false).bit_count(),
        )

    def automatic_rank(self, detected, false):
        p, n = self.present.measure(detected, latest=True), self.absent.measure(false)
        miss = (
            1 - p["weighted_active_seconds"] / p["weighted_seconds"]
            if p["weighted_seconds"]
            else 0.0
        )
        false_rate = (
            n["weighted_active_seconds"] / n["weighted_seconds"] if n["weighted_seconds"] else 0.0
        )
        return round(_error_cost(miss, false_rate), 10), round(miss, 12), round(false_rate, 10)


def _error_cost(missed_fraction, false_fraction):
    """Class-normalized time cost: missed presence matters more, never infinitely."""
    return 100 * (MISSED_TIME_COST * max(0.0, missed_fraction) + max(0.0, false_fraction))

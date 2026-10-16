"""Elapsed-time calibration scores over observed sessions, never a live detector."""

from functools import cached_property, lru_cache

from .constants import MISSED_TIME_COST
from .intervals import WeightedTime
from .scoring import error_cost, mass
from .timing import _sessions, hit_runs, make_window


def _clip(intervals, start, end):
    return [(max(a, start), min(b, end)) for a, b in intervals if b > start and a < end]


def _raw_spans(window, bits):
    """``(starts, ends)`` of the runs of hits: each from halfway to the sample before to
    halfway to the sample after."""
    if hasattr(window, "raw"):
        return window.raw(bits)
    times = window.times
    runs = hit_runs(bits)
    return (
        [(times[first - 1] + times[first]) / 2 if first else times[0] for first, _ in runs],
        [
            (times[stop - 1] + times[stop]) / 2 if stop < len(times) else times[-1]
            for _, stop in runs
        ],
    )


def _recent_start(windows):
    spans = [(w.start, w.end) for _, w, _ in windows]
    spans = [(a, b) for a, b in spans if b > a]
    remaining = sum(b - a for a, b in spans) * 0.8
    for start, end in spans:
        if remaining <= end - start:
            return start + remaining
        remaining -= end - start
    return 0


def _confidence_time(rows):
    """Assign observations midpoint cells; dense telemetry adds no time."""
    times = [rows[0][0]] + [(a[0] + b[0]) / 2 for a, b in zip(rows, rows[1:], strict=False)]
    if getattr(rows, "ends", None) is not None and rows.ends[0] is not None:
        times = [row[0] for row in rows] + [rows.ends[-1]]
    confidence = WeightedTime(times, [mass(row) for row in rows] + [0.0])
    # Candidate spans reuse the same sample midpoints and timing-shifted endpoints.
    # Remember their exact prefix values rather than locating each one millions of times.
    confidence.at = lru_cache(maxsize=4096)(confidence.at)
    return confidence


def _empty_measure():
    return {
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


def _merge_measure(total, contribution):
    """Aggregate in session order, with the same additions as a full replay."""
    for key, value in contribution.items():
        if key == "longest_inactive_seconds":
            total[key] = max(total[key], value)
        else:
            total[key] += value


def _measurement_windows(windows, recent_start, recent):
    """Select sessions with observable time; warm-up-only mass is invariant in hits."""
    measured, unscored = [], 0
    for index, (_, window, _) in enumerate(windows):
        end = window.end
        if recent and end < recent_start:
            continue
        start = window.start
        if recent:
            start = max(start, recent_start)
        if end <= start:
            unscored += len(window.times)
        else:
            measured.append(index)
    return measured, unscored


class DurationGroup:
    def __init__(self, rows, config=None, *, absent=False, barriers=(), measured=True):
        """``measured`` False for a group that only replays hit masks (``_spans``): its windows
        keep no time integrals (``measure``), which are as large as the samples again."""
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
            (offset, make_window(part, hold, on, off), _confidence_time(part) if measured else None)
            for offset, part in _sessions(rows, barriers)
        ]
        self.unscored_mask = sum(
            window.mask << offset
            for offset, window, _ in self.windows
            if window.end <= window.start
        )
        self.recent_start = _recent_start(self.windows)
        self.measured_windows = tuple(
            _measurement_windows(self.windows, self.recent_start, recent)
            for recent in (False, True)
        )
        # Distinct positive/negative pairs often share one class mask. Keep a bounded
        # sweep's per-class summaries even when the complete-rank pair is new.
        self.measure = lru_cache(maxsize=8192)(self._measure)
        # Changing a global mask commonly leaves most recording sessions unchanged.
        # One bounded cache across this group avoids unbounded per-session caches.
        self.part = lru_cache(maxsize=8192)(self._measure_part)
        # Automatic ranking consumes only confidence mass. Retain scalar local
        # contributions, avoiding nine-field duration aggregation per candidate.
        self.weighted = lru_cache(maxsize=8192)(self._weighted)
        self.weighted_part = lru_cache(maxsize=8192)(self._weighted_part)

    def _spans(self, window, bits, latest):
        if not self.timed:
            return _raw_spans(window, bits)
        if self.absent:
            return window._upper(bits)
        return window._lower(bits) if latest else window._possible_onset(bits)

    def _intervals(self, window, bits, latest):
        return list(zip(*self._spans(window, bits, latest), strict=True))

    def _measure(self, bits, recent=False, latest=False, *, weighted=True):
        result = _empty_measure()
        indices, result["unscored_samples"] = self.measured_windows[bool(recent)]
        for index in indices:
            offset, window, _ = self.windows[index]
            _merge_measure(
                result, self.part(index, (bits >> offset) & window.mask, recent, latest, weighted)
            )
        return result

    def _measure_part(self, index, bits, recent, latest, weighted=True):
        _, window, confidence = self.windows[index]
        result = _empty_measure()
        start = window.start
        if recent:
            start = max(start, self.recent_start)
        end = window.end
        if end <= start:
            result["unscored_samples"] = len(window.times)
        else:
            intervals = _clip(self._intervals(window, bits, latest), start, end)
            _add_window(result, start, end, intervals, confidence, weighted=weighted)
        return result

    @cached_property
    def window_mass(self):
        masses = {}
        for index in self.measured_windows[0][0]:
            _, window, confidence = self.windows[index]
            start = window.start
            masses[index] = confidence.between(start, window.end)
        return masses

    @cached_property
    def weighted_seconds(self):
        total = 0.0
        for value in self.window_mass.values():
            total += value
        return total

    def _weighted(self, bits, latest=False):
        active = 0.0
        for index in self.measured_windows[0][0]:
            offset, window, _ = self.windows[index]
            # Keep original session addition order; sum() can use different
            # floating accumulation than these chronological additions.
            active += self.weighted_part(index, (bits >> offset) & window.mask, latest)
        return self.weighted_seconds, active

    def _weighted_part(self, index, bits, latest):
        _, window, confidence = self.windows[index]
        if not bits:
            return 0.0
        if (
            bits == window.mask
            and min(window.hold, window.on, window.off) >= 0
            and window.start >= window.times[0] + window.on
        ):
            # A full run covers the scored session: its onset is no later
            # than warm-up and its held tail reaches the last observation.
            return self.window_mass[index]
        start = window.start
        intervals = _clip(self._intervals(window, bits, latest), start, window.end)
        return sum(confidence.between(a, b) for a, b in intervals)


def _add_window(result, start, end, intervals, confidence, *, weighted=True):
    result["observed_seconds"] += end - start
    result["active_seconds"] += sum(b - a for a, b in intervals)
    if weighted:
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
        # A full gate sweep evicts the small per-measure cache. Complete ranks depend
        # only on these immutable replay inputs and the two hit masks; repeated seeds
        # and repair candidates can therefore reuse them without another duration replay.
        self.rank = lru_cache(maxsize=8192)(self.rank)
        self.automatic_rank = lru_cache(maxsize=4096)(self.automatic_rank)

    def summary(self, detected, false, recent=False):
        # Reported duration fields are unweighted. Confidence only contributes to
        # automatic_rank; do not integrate it for every human search candidate.
        present = self.present.measure(detected, recent, weighted=False)
        latest = self.present.measure(detected, recent, True, weighted=False)
        absent = self.absent.measure(false, recent, weighted=False)
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
            "error_cost": error_cost(
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
        p, n = self.present.weighted(detected, True), self.absent.weighted(false)
        miss = 1 - p[1] / p[0] if p[0] else 0.0
        false_rate = n[1] / n[0] if n[0] else 0.0
        return round(error_cost(miss, false_rate), 10), round(miss, 12), round(false_rate, 10)

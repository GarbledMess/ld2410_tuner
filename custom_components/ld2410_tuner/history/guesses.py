"""Automatic guesses over native half-open physical coverage intervals.

Each run is (start, end, state, level, basis). Native ingestion records guess changes
and ends of coverage in day files; queries never carry an ended guess into a new
unguessed reading. Evidence applies calibrated confidence and the quiet-baseline veto.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right

from ..calibration.constants import (
    GUESS_CONFIDENCE_DELTA,
    GUESS_CONFIDENCE_STEP,
    MIN_AUTO_CONFIDENCE,
    SAMPLE_SECONDS,
)
from . import day_results, daystore, trust

GAP = SAMPLE_SECONDS * 2  # Samples further apart than this share no cell: a recording gap.
PERCENT = round(GUESS_CONFIDENCE_STEP * 100)
DELTA = round(GUESS_CONFIDENCE_DELTA * 100)  # Percent a sample may differ from its run's level.
FLOOR = round(MIN_AUTO_CONFIDENCE * 100)  # Percent under which a guess is not trained on.
CODES = {"present": 1, "not_present": 2}
UNKNOWN, REFERENCE, EXTERNAL = range(3)  # Where a guess came from; UNKNOWN: migrated rows.


def level(percent):
    """A confidence in percent as whole steps. Rounding never lifts a confidence under the
    ``MIN_AUTO_CONFIDENCE`` floor onto it (the floor is a whole number of steps)."""
    steps = round(max(0, min(100, percent)) / PERCENT)
    return min(steps, FLOOR // PERCENT - 1) if percent < FLOOR else steps


def guess(state, percent, basis=UNKNOWN):
    """A sample's guess as ``(state, percent, basis)`` (``percent`` clamped to 0-100), None
    when it has none: a state other than present or not_present, or a confidence under one
    step."""
    percent = max(0, min(100, percent))
    return (state, percent, basis) if state in CODES and level(percent) else None


def confidence(level):
    return round(level * GUESS_CONFIDENCE_STEP, 4)


def continues(run, sample_guess):
    """Whether a sample's guess belongs to ``run``: the same state and basis, and a confidence
    less than ``DELTA`` percent from the run's."""
    return bool(
        sample_guess
        and (sample_guess[0], sample_guess[2]) == (run[2], run[4])
        and abs(sample_guess[1] - run[3] * PERCENT) < DELTA
    )


def _label(run):
    return run[2], "automatic", confidence(run[3])


def _confident(run):
    return run[1] > run[0] and confidence(run[3]) >= MIN_AUTO_CONFIDENCE


def _owns_sample(run, timestamp):
    """A live run may start at the snapshot time without covering elapsed time yet."""
    return run[0] <= timestamp < run[1] or run[0] == run[1] == timestamp


class Guesses:
    """One radar's guess runs in time order, with bisect lookups.

    ``at(t)`` is the confident guess in force at ``t``; ``of_sample(t)`` returns
    the run regardless of confidence. Both use half-open physical intervals."""

    __slots__ = ("runs", "starts", "ends", "_labels")

    def __init__(self, runs=()):
        self.runs = sorted(runs)
        self.starts = [run[0] for run in self.runs]
        self.ends = [run[1] for run in self.runs]
        self._labels = None

    def labels(self):
        """Each run's ``(state, "automatic", confidence)``, None when it is under the
        ``MIN_AUTO_CONFIDENCE`` floor: built on first use, one shared tuple per state and
        level, so a lookup is a bisect and an index."""
        if self._labels is None:
            known = {}
            self._labels = [
                known.setdefault((run[2], run[3]), _label(run))
                if confidence(run[3]) >= MIN_AUTO_CONFIDENCE
                else None
                for run in self.runs
            ]
        return self._labels

    def __bool__(self):
        return any(map(_confident, self.runs))

    def __eq__(self, other):
        return isinstance(other, Guesses) and self.runs == other.runs

    __hash__ = None

    def of_sample(self, timestamp):
        index = bisect_right(self.starts, timestamp) - 1
        return (
            self.runs[index] if index >= 0 and _owns_sample(self.runs[index], timestamp) else None
        )

    def sample_guess(self, timestamp):
        """``(state, "automatic", confidence)`` of a recorded sample's own guess when it is at
        least ``MIN_AUTO_CONFIDENCE`` (the learner's floor); None otherwise."""
        index = bisect_right(self.starts, timestamp) - 1
        if index < 0 or not _owns_sample(self.runs[index], timestamp):
            return None
        return self.labels()[index]

    def percents(self, timestamps):
        """The confidence in percent (a whole number of steps; 0 for none) of the guess of each
        of the recorded samples ``timestamps`` (ascending), in one walk over the runs."""
        index, last = -1, len(self.runs) - 1
        for timestamp in timestamps:
            while index < last and self.starts[index + 1] <= timestamp:
                index += 1
            inside = index >= 0 and _owns_sample(self.runs[index], timestamp)
            yield self.runs[index][3] * PERCENT if inside else 0

    def at(self, timestamp, default=None):
        index = bisect_right(self.starts, timestamp) - 1
        if index < 0 or timestamp >= self.ends[index]:
            return default  # Inside a run, so the run is not empty.
        return self.labels()[index] or default

    def ending_at(self, timestamp):
        """The run that ends exactly at ``timestamp``, if any."""
        index = bisect_left(self.ends, timestamp)
        return (
            self.runs[index] if index < len(self.ends) and self.ends[index] == timestamp else None
        )

    def between(self, start, end):
        """The runs that overlap ``[start, end)``."""
        return self.runs[bisect_right(self.ends, start) : bisect_left(self.starts, end)]

    def cells(self, start=float("-inf"), end=float("inf")):
        """``(start, end, (state, "automatic", confidence))`` of each confident run in
        ``[start, end)``: the spans an automatic room estimate reads."""
        return [
            (run[0], run[1], _label(run)) for run in self.between(start, end) if _confident(run)
        ]


EMPTY = Guesses()


def counted(sample_guess, row, histograms):
    """The calibrated confidence a sample's guess ``(state, percent, basis)`` carries as
    evidence, None when it carries none (``_trusted``), or when it is a radar's present guess
    while ``row`` is quiet against the radar's ``histograms``."""
    if sample_guess is None:
        return None
    state, percent, basis = sample_guess
    found = _trusted(state, confidence(level(percent)), basis)
    if found is None:
        return None
    if state == "present" and basis != EXTERNAL and _vetoed(row, trust.ceilings(histograms)):
        return None
    return found


def _trusted(state, recorded, basis):
    """The confidence a guess of ``recorded`` confidence carries as evidence: a radar's own,
    calibrated (``trust.trusted``); an external source's, as it gave it. None under the
    ``MIN_AUTO_CONFIDENCE`` floor, recorded or calibrated: the radar was not confident, or the
    calibration says its kind of guess is mostly wrong."""
    if recorded < MIN_AUTO_CONFIDENCE:
        return None
    found = recorded if basis == EXTERNAL else trust.trusted(state, recorded)
    return found if found >= MIN_AUTO_CONFIDENCE else None


def _vetoed(row, limits):
    return limits is not None and trust.quiet(row, limits)


def recorded(runtime, device_id, since=float("-inf")):
    """The day store's runs ending at or after ``since`` (the live day as of the view)."""
    days = getattr(runtime, "_days", None)
    if days is None:
        return []
    until = getattr(runtime, "_history_until", None)
    found = []
    for date in days.days(device_id):
        if daystore.bounds(date)[1] > since:
            runs = day_results.remembered(
                days,
                device_id,
                date,
                "guesses",
                (),
                lambda date=date: _day_runs(days, device_id, date, until),
            )
            found.extend(run for run in runs if run[1] >= since)
    return _merged(found)


def _day_runs(days, device_id, date, until):
    day = days.day(device_id, date, until)
    return tuple(day.guesses) if day else ()


def of(runtime, device_id, since=float("-inf")):
    """``Guesses`` of ``device_id``: its recorded runs ending at or after ``since``."""
    return Guesses(recorded(runtime, device_id, since))


def evidence(runtime, device_id, since=float("-inf")):
    """``Guesses`` of ``device_id`` as evidence (see ``counted``): each radar run at its
    calibrated confidence and, for present, without the samples where the radar was quiet;
    runs that no longer reach the floor are gone, and neighbours that now match are one run."""
    device = runtime.data["devices"].get(device_id) or {}
    kept = [
        (*run[:3], level(found * 100), run[4])
        for run in of(runtime, device_id, since).runs
        if (found := _trusted(run[2], confidence(run[3]), run[4])) is not None
    ]
    limits = trust.ceilings(device.get("histograms"))
    return Guesses(_merged(kept if limits is None else _loud(kept, runtime, device, limits)))


def _loud(runs, runtime, device, limits):
    """``runs`` with the recorded time where a radar's present guess read quiet cut out
    exactly. Unrecorded time contradicts nothing, so it stays in the run."""
    if getattr(runtime, "_days", None) is None:
        return runs
    device_id = next(key for key, value in runtime.data["devices"].items() if value is device)
    result = []
    for run in runs:
        if run[2] != "present" or run[4] == EXTERNAL:
            result.append(run)
        else:
            result.extend(_loud_parts(run, _quiet(runtime, device_id, run, limits)))
    return result


def _quiet(runtime, device_id, run, limits):
    """The recorded ``(start, end)`` within ``run`` where every gate read within its quiet
    ceiling (``trust.quiet``); a missing reading is not a quiet one."""
    from . import spans

    for span in spans.events(runtime, device_id, start=run[0], end=run[1]):
        if trust.quiet(tuple(255 if value is None else value for value in span.values), limits):
            yield span.start, span.end


def _loud_parts(run, quiet):
    left = run[0]
    for start, end in quiet:
        if start > left:
            yield (left, start, *run[2:])
        left = end
    if left < run[1]:
        yield (left, run[1], *run[2:])


def _merged(runs):
    """``runs`` in time order, those that meet and match as one."""
    merged = []
    for run in sorted(runs):
        if merged and merged[-1][1] == run[0] and merged[-1][2:] == run[2:]:
            merged[-1] = (merged[-1][0], run[1], *run[2:])
        else:
            merged.append(run)
    return merged

"""A radar's automatic guesses, kept as change runs instead of a code in every sample.

A run is ``(start, end, state, level, basis)``: the radar guessed ``state`` ("present" or
"not_present") with confidence ``level * GUESS_CONFIDENCE_STEP`` over ``[start, end)``, from
``basis`` (references or an external source). A sample's guess covers the cell around it: from
the midpoint with the sample before to the midpoint with the sample after, or just to the sample
itself across a recording gap (more than ``GAP`` apart). A run is the union of consecutive
samples' cells that share one guess; a new run starts when the state or the basis changes,
after a gap, or when a sample's confidence is ``GUESS_CONFIDENCE_DELTA`` or more away from the
run's, so a confidence hovering on a step edge stays one run. The run's level is its first
sample's confidence in steps, and under the ``MIN_AUTO_CONFIDENCE`` floor whenever that
sample's raw confidence is.

Runs are stored per device (``device["guess_runs"]``) in chunks of ``CHUNK_RUNS``, coded like
history blocks. The runs since the last flush wait in memory (pending runs); the open one, which
recording still extends, stays there until it closes, and saving adds it to a copy
(``upsert``). This module is the only reader and writer of them: ``Guesses`` answers "guess at
t" and "runs in [a, b)". ``of`` reads them as recorded; ``evidence`` reads them as the learner,
the references and the room estimate use them (``trust``): a radar's guess at its calibrated
confidence, a present one only where the radar's quiet baseline does not veto it. ``counted``
applies the same rules to a sample as it is recorded.
"""

from __future__ import annotations

import struct
import zlib
from bisect import bisect_left, bisect_right
from functools import lru_cache
from itertools import groupby
from operator import itemgetter

from ..calibration.constants import (
    GUESS_CONFIDENCE_DELTA,
    GUESS_CONFIDENCE_STEP,
    MIN_AUTO_CONFIDENCE,
    SAMPLE_SECONDS,
)
from . import trust
from .codec import decode_columns, encode_payload

GAP = SAMPLE_SECONDS * 2  # Samples further apart than this share no cell: a recording gap.
PERCENT = round(GUESS_CONFIDENCE_STEP * 100)
DELTA = round(GUESS_CONFIDENCE_DELTA * 100)  # Percent a sample may differ from its run's level.
FLOOR = round(MIN_AUTO_CONFIDENCE * 100)  # Percent under which a guess is not trained on.
STATES = (None, "present", "not_present")  # Stored state codes.
CODES = {"present": 1, "not_present": 2}
UNKNOWN, REFERENCE, EXTERNAL = range(3)  # Where a guess came from; UNKNOWN: migrated rows.
RUN = struct.Struct(">ddBBB")  # start, end, state code, level, basis.
CHUNK_RUNS = 256
VERSION = 1


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


def from_code(code, percent):
    """The guess an old-layout row held: a state code and a confidence in percent."""
    valid = code < len(STATES) and 1 <= percent <= 100
    return guess(STATES[code], percent) if valid else None


def confidence(level):
    return round(level * GUESS_CONFIDENCE_STEP, 4)


def advance(runs, previous, timestamp, sample_guess):
    """Add the sample at ``timestamp`` to ``runs`` (lists, oldest first, the last one open while
    its end is the previous sample). ``previous`` is the sample before it (None for the first);
    ``sample_guess`` is its ``guess``."""
    near = previous is not None and timestamp - previous <= GAP
    current = runs[-1] if runs and runs[-1][1] == previous else None
    if current is not None and near:
        if _continues(current, sample_guess):
            current[1] = timestamp
            return
        current[1] = (previous + timestamp) / 2
    if sample_guess:
        state, percent, basis = sample_guess
        start = (previous + timestamp) / 2 if near else timestamp
        runs.append([start, timestamp, state, level(percent), basis])


def _continues(run, sample_guess):
    """Whether a sample's guess belongs to ``run``: the same state and basis, and a confidence
    less than ``DELTA`` percent from the run's."""
    return bool(
        sample_guess
        and (sample_guess[0], sample_guess[2]) == (run[2], run[4])
        and abs(sample_guess[1] - run[3] * PERCENT) < DELTA
    )


def split_open(runs, newest):
    """``(closed, open)``: the runs recording can no longer extend, and the last one while it
    still ends at the newest sample ``newest``."""
    if runs and runs[-1][1] == newest:
        return runs[:-1], runs[-1:]
    return runs, []


def from_samples(samples):
    """The runs of ``(timestamp, guess)`` samples in time order (``advance`` over them)."""
    runs, previous = [], None
    for timestamp, sample_guess in samples:
        advance(runs, previous, timestamp, sample_guess)
        previous = timestamp
    return [tuple(run) for run in runs]


def _chunk(part):
    raw = b"".join(
        RUN.pack(start, end, CODES[state], level, basis) for start, end, state, level, basis in part
    )
    return {
        "version": VERSION,
        "start": part[0][0],
        "end": max(run[1] for run in part),
        "count": len(part),
        "data": encode_payload(raw, RUN.size),
    }


def encode(runs):
    """Stored chunks holding ``runs`` (sorted by start)."""
    return [_chunk(runs[index : index + CHUNK_RUNS]) for index in range(0, len(runs), CHUNK_RUNS)]


@lru_cache(maxsize=64)  # A chunk is read once per job; the panel rereads the newest ones.
def _decoded(data, count):
    raw = decode_columns(data, RUN.size, count)
    return tuple(
        (start, end, STATES[code], level, basis)
        for start, end, code, level, basis in RUN.iter_unpack(raw)
        if code in CODES.values() and level
    )


def decode(chunk):
    """The runs of one stored chunk; none when it cannot be read."""
    try:
        if chunk["version"] != VERSION:
            return ()
        return _decoded(chunk["data"], int(chunk["count"]))
    except (KeyError, TypeError, ValueError, zlib.error, struct.error):
        return ()


def upsert(chunks, runs):
    """``chunks`` with ``runs`` (sorted by start) merged in: a run replaces a stored one that
    starts at the same time. Only the chunks the runs reach are decoded and re-coded."""
    if not runs:
        return chunks
    starts = [chunk["start"] for chunk in chunks]
    low = max(bisect_right(starts, runs[0][0]) - 1, 0)
    high = bisect_right(starts, max(run[1] for run in runs))
    merged = {run[0]: tuple(run) for chunk in chunks[low:high] for run in decode(chunk)}
    merged.update((run[0], tuple(run)) for run in runs)
    return [*chunks[:low], *encode(sorted(merged.values())), *chunks[high:]]


def join(known, open_runs, runs):
    """``runs`` (new, sorted, from ``from_samples``) with the first one extended back over the
    run of ``known`` it continues, if that run has the same guess and is not one of
    ``open_runs`` (recording still extends those, so they must keep their start)."""
    before = known.ending_at(runs[0][0]) if runs else None
    if before and before[2:] == runs[0][2:] and before[0] not in {run[0] for run in open_runs}:
        return [(before[0], *runs[0][1:]), *runs[1:]]
    return runs


def kept(runs, times):
    """The ``runs`` that hold at least one of the sorted ``times``: a run lives while any of
    the samples it covers does, so runs are retained and thinned with the history rows."""
    return [run for run in runs if bisect_left(times, run[0]) < bisect_right(times, run[1])]


def _label(run):
    return run[2], "automatic", confidence(run[3])


def _confident(run):
    return run[1] > run[0] and confidence(run[3]) >= MIN_AUTO_CONFIDENCE


class Guesses:
    """One radar's guess runs in time order, with bisect lookups.

    ``at(t)`` is the guess in force at time ``t`` (a Timeline of the cells, below the
    ``MIN_AUTO_CONFIDENCE`` floor no guess); ``of_sample(t)`` the run a recorded sample
    belongs to, whatever its confidence (its cell includes its own time at both ends)."""

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
        return self.runs[index] if index >= 0 and timestamp <= self.ends[index] else None

    def sample_guess(self, timestamp):
        """``(state, "automatic", confidence)`` of a recorded sample's own guess when it is at
        least ``MIN_AUTO_CONFIDENCE`` (the learner's floor); None otherwise."""
        index = bisect_right(self.starts, timestamp) - 1
        if index < 0 or timestamp > self.ends[index]:
            return None
        return self.labels()[index]

    def percents(self, timestamps):
        """The confidence in percent (a whole number of steps; 0 for none) of the guess of each
        of the recorded samples ``timestamps`` (ascending), in one walk over the runs."""
        index, last = -1, len(self.runs) - 1
        for timestamp in timestamps:
            while index < last and self.starts[index + 1] <= timestamp:
                index += 1
            inside = index >= 0 and timestamp <= self.ends[index]
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


def pending(runtime, device_id):
    """``device_id``'s runs since the last flush, the open one last (lists, still extended)."""
    return runtime._history_runtime.get(device_id, {}).get("runs", [])


def stored(runtime, device_id, since=float("-inf")):
    """The runs held in ``device_id``'s stored chunks that end at or after ``since``."""
    device = runtime.data["devices"].get(device_id) or {}
    return [
        run
        for chunk in device.get("guess_runs", [])
        if _reaches(chunk, since)
        for run in decode(chunk)
        if run[1] >= since
    ]


def _reaches(chunk, since):
    try:
        return chunk["end"] >= since
    except (KeyError, TypeError):
        return False


def of(runtime, device_id, since=float("-inf")):
    """``Guesses`` of ``device_id``: stored runs ending at or after ``since``, and the pending
    runs, which replace a stored run that starts with them (the open run is stored too)."""
    merged = {run[0]: run for run in stored(runtime, device_id, since)}
    merged.update((run[0], tuple(run)) for run in pending(runtime, device_id))
    return Guesses(merged.values())


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
    """``runs`` with the samples where a radar's present guess was quiet cut out of it. A run
    with no retained sample is left as it is: nothing contradicts it."""
    present = [run for run in runs if run[2] == "present" and run[4] != EXTERNAL]
    if not present:
        return runs
    starts = [run[0] for run in present]
    samples = runtime._iter_history_samples(device, since=starts[0], until=present[-1][1])
    found = (
        (index, ts, trust.quiet(row, limits))
        for ts, row in samples
        if (index := bisect_right(starts, ts) - 1) >= 0 and ts <= present[index][1]
    )
    cut = {
        index: _pieces(present[index], [(ts, quiet) for _, ts, quiet in group])
        for index, group in groupby(found, itemgetter(0))
    }
    replaced = {present[index] for index in cut}
    return [run for run in runs if run not in replaced] + [
        piece for pieces in cut.values() for piece in pieces
    ]


def _pieces(run, samples):
    """The parts of ``run`` that are loud: from the midpoint with the quiet sample before to the
    midpoint with the quiet sample after (the run's own ends at its ends). ``samples`` are the
    ``(time, quiet)`` of its retained samples."""
    pieces, first = [], None
    for index, (_, quiet) in enumerate([*samples, (None, True)]):
        if not quiet and first is None:
            first = index
        elif quiet and first is not None:
            start = run[0] if first == 0 else (samples[first - 1][0] + samples[first][0]) / 2
            stop = (
                run[1] if index == len(samples) else (samples[index - 1][0] + samples[index][0]) / 2
            )
            pieces.append((start, stop, *run[2:]))
            first = None
    return pieces


def _merged(runs):
    """``runs`` in time order, those that meet and match as one."""
    merged = []
    for run in sorted(runs):
        if merged and merged[-1][1] == run[0] and merged[-1][2:] == run[2:]:
            merged[-1] = (merged[-1][0], run[1], *run[2:])
        else:
            merged.append(run)
    return merged

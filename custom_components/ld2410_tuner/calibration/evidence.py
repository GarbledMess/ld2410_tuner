"""Collect the same bounded, labelled recordings for fitting and comparison."""

from collections import deque

from ..const import HISTORY_KEYS
from ..history import guesses, recording
from ..history.labels import _history_label_reader
from ..rooms import attribution
from .constants import AUTO_SNIPPET_ROWS, MAX_CLASS_SAMPLES, SAMPLE_SECONDS

LABELS = ("present", "not_present")
GAP = SAMPLE_SECONDS * 2  # Samples further apart start a new session (``timing._sessions``).


def collect_samples(runtime, device_id, keys):
    """``(human, automatic)`` samples, the newest ``MAX_CLASS_SAMPLES`` of each class, whatever
    the age of the history. Automatic ones are read from stretches of one automatic label (the
    radar's guess, or the room's estimate): snippets of ``AUTO_SNIPPET_ROWS`` consecutive
    samples, spaced so that the history yields about ``2 * MAX_CLASS_SAMPLES`` of them, each
    carrying the number of samples it stands for, so a stretch counts for its duration and a
    recording gap for nothing."""
    device = runtime.data["devices"][device_id]
    groups = {label: deque(maxlen=MAX_CLASS_SAMPLES) for label in LABELS}
    stretches = _Stretches(_period(runtime, device_id))
    indices = [HISTORY_KEYS.index(key) for key in keys]
    label_at = _history_label_reader(device)
    own = guesses.evidence(runtime, device_id)
    # The radar's own label wins; only then do attributed room labels apply (spec 4.3).
    # Human room Present attributed by the radar's baseline (rule 2) is a guess, not human.
    # Level-4 automatic room truth (sensor labels, then the estimate from every member's
    # recorded guess, this radar's own included) trains as a confidence-weighted guess; the
    # learner applies the automatic evidence weight setting to it.
    room = attribution.select(
        attribution.room_targets(runtime, device_id), attribution.auto_targets(runtime, device_id)
    )
    for ts, row in runtime._iter_history_samples(device):
        if not any(row[index] <= 100 for index in indices):
            continue
        label, guess = _sample_label(ts, label_at, room, own)
        if label in groups:
            groups[label].append((ts, row, label))
        stretches.add(ts, row, guess)
    rows = [(ts, _values(row), label) for group in groups.values() for ts, row, label in group]
    return rows, stretches.finish()


def _period(runtime, device_id):
    """Samples from one snippet's start to the next: all of them for a history that fits
    twice the class cap, else spaced to fit."""
    samples = recording.history_summary(runtime, device_id)[1]
    samples += len(recording.pending_samples(runtime, device_id))
    return AUTO_SNIPPET_ROWS * max(1, -(-samples // (2 * MAX_CLASS_SAMPLES)))


def _values(row):
    return {key: row[i] for i, key in enumerate(HISTORY_KEYS) if row[i] <= 100}


class _Stretches:
    """The automatic samples kept from each stretch of consecutive samples with one label."""

    def __init__(self, period):
        self.period = period
        self.groups = {label: deque(maxlen=MAX_CLASS_SAMPLES) for label in LABELS}
        self.label = self.last = None
        self.seen, self.kept = 0, []

    def add(self, ts, row, guess):
        """The sample at ``ts`` with its automatic ``guess`` (None: it has none)."""
        label = guess[0] if guess else None
        if label != self.label or (self.last is not None and ts - self.last > GAP):
            self._close()
            self.label = label
        if label is None:
            return
        if self.seen % self.period < AUTO_SNIPPET_ROWS:
            self.kept.append((ts, row, guess[2]))
        self.seen += 1
        self.last = ts

    def _close(self):
        """End the stretch: the samples kept stand for all those it saw."""
        if self.kept:
            weight = self.seen / len(self.kept)
            self.groups[self.label].extend(
                (ts, _values(row), self.label, confidence, weight)
                for ts, row, confidence in self.kept
            )
        self.label, self.last, self.seen, self.kept = None, None, 0, []

    def finish(self):
        self._close()
        return [sample for group in self.groups.values() for sample in group]


def _sample_label(ts, label_at, room, own):
    """``(label, guess)``: a human label, else an automatic room guess where one exists, else
    the radar's own recorded guess."""
    label = label_at(ts)
    if label is not None:
        return label, None
    decided = room(ts) if room is not None else None
    if attribution.is_human(decided):
        return decided[0], None
    return None, decided or own.sample_guess(ts)

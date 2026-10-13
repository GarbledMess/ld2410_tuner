"""Attribution (spec 4.3): which member radars saw each occupied episode of a group."""

from __future__ import annotations

import re
import time
from array import array
from bisect import bisect_left, bisect_right
from collections.abc import Iterable
from copy import deepcopy
from functools import lru_cache
from itertools import chain, compress, islice, pairwise, repeat
from operator import gt, le, sub

from ..calibration.constants import (
    ATTRIBUTION_FULL_CONFIDENCE_SAMPLES,
    ATTRIBUTION_MARGIN,
    ATTRIBUTION_SUSTAIN_SECONDS,
    ATTRIBUTION_TAIL_QUANTILE,
    MIN_AUTO_CONFIDENCE,
    SAMPLE_SECONDS,
)
from ..const import HISTORY_KEYS, TRAINING_FIELDS
from ..history import guesses, recording
from ..history.labels import _history_label_reader, index_reader, label_index
from ..presence.inference import MIN_REFERENCE
from . import groups, truth

GAP = guesses.GAP
MEMBERSHIP_TTL = 300.0
LABEL_KEYS = ("history_labels", *TRAINING_FIELDS)
# Human room truth never depends on member guesses, so per-radar targets skip them.
NO_GUESSES = guesses.EMPTY


def _state(label):
    return label[0] if label else None


STRIDE = len(HISTORY_KEYS)
MISSING = 255  # What a gate that did not read is kept as.
UNREAD = bytes(range(101, 256))  # The values that are no reading.
ONES = re.compile(b"\x01+")


class Samples:
    """A radar's samples in time order, kept as two flat buffers: the sample ``times`` and, one
    sample after another, the energies of the gates in ``keys`` (``MISSING`` where a gate did not
    read). A few bytes a sample, where a row of gates in a dictionary took a kilobyte."""

    __slots__ = ("keys", "times", "matrix", "_gaps")

    def __init__(self, keys, times, matrix):
        self.keys, self.times, self.matrix, self._gaps = keys, times, matrix, None

    @classmethod
    def recorded(cls, pairs):
        """The samples ``(timestamp, row)`` of stored history, whatever their order; a row is the
        energies of ``HISTORY_KEYS``, and a missing one a gate that did not read."""
        times, matrix = array("d"), bytearray()
        for timestamp, row in pairs:
            row = bytes(row[:STRIDE])
            times.append(timestamp)
            matrix += row if len(row) == STRIDE else row.ljust(STRIDE, bytes([MISSING]))
        return cls._in_order(times, bytes(matrix))

    @classmethod
    def read(cls, batches):
        """The samples of ``(times, columns)`` batches (``_history_columns``), whatever their
        order: each batch's columns are interleaved into rows, in C."""
        times, parts = array("d"), []
        for batch_times, columns in batches:
            times.extend(batch_times)
            rows = bytearray(len(batch_times) * STRIDE)
            for position, column in enumerate(columns):
                rows[position::STRIDE] = column
            parts.append(rows)
        return cls._in_order(times, b"".join(parts))

    @classmethod
    def _in_order(cls, times, matrix):
        if not all(map(le, times, islice(times, 1, None))):  # Ties stay in the order read.
            order = sorted(range(len(times)), key=times.__getitem__)
            times = array("d", map(times.__getitem__, order))
            matrix = b"".join(matrix[i * STRIDE : (i + 1) * STRIDE] for i in order)
        return cls(tuple(HISTORY_KEYS), times, matrix)

    def __len__(self):
        return len(self.times)

    def __getitem__(self, span):
        first, stop, _ = span.indices(len(self))
        width = len(self.keys)
        return Samples(self.keys, self.times[first:stop], self.matrix[first * width : stop * width])

    def between(self, first, last):
        """The samples from time ``first`` to time ``last``, both included."""
        return self[bisect_left(self.times, first) : bisect_right(self.times, last)]

    def column(self, key):
        """One gate's energies, a byte a sample."""
        if key not in self.keys:
            return bytes([MISSING]) * len(self)
        return self.matrix[self.keys.index(key) :: len(self.keys)]

    @property
    def gaps(self):
        """Where the recording has a gap: the indices of samples further than ``GAP`` apart
        from the one before."""
        if self._gaps is None:
            self._gaps = list(
                compress(
                    range(1, len(self)),
                    map(gt, map(sub, islice(self.times, 1, None), self.times), repeat(GAP)),
                )
            )
        return self._gaps


def stretches(count, cuts):
    """``(first, stop)`` of each stretch of ``count`` samples that a reader answers alike:
    split at the sample indices ``cuts`` (in order); None, where the reader cannot say, makes
    each sample a stretch."""
    if cuts is None:
        return zip(range(count), range(1, count + 1), strict=True)
    return pairwise([0, *(cut for cut in cuts if 0 < cut < count), count]) if count else iter(())


def change_points(times, reader):
    """The indices of the samples ``times`` that ``reader`` can answer differently from the
    sample before, in order; None when it cannot say. A reader says by ``cuts`` (the indices,
    for these times), by ``edges`` (the times, in order, at which it can change) or by
    ``parts`` (the readers it is made of)."""
    cuts = getattr(reader, "cuts", None)
    if cuts is not None:
        return cuts
    edges = getattr(reader, "edges", None)
    if edges is not None:
        return _cuts(times, edges)
    parts = getattr(reader, "parts", None)
    found = None if parts is None else [change_points(times, part) for part in parts]
    return None if found is None or None in found else sorted(set(chain.from_iterable(found)))


def episodes(truth_at, times: Iterable[float], gap=GAP) -> list[tuple[float, float]]:
    """Maximal runs of Present truth over ``times``, split at recording gaps. ``truth_at`` is
    read once for each stretch of samples that it answers alike (``change_points``)."""
    count = len(times)
    breaks = list(
        compress(range(1, count), map(gt, map(sub, islice(times, 1, None), times), repeat(gap)))
    )
    found, begin, stop = [], None, 0
    for first, after in stretches(count, change_points(times, truth_at)):
        label = truth_at(times[first])
        if label and label[0] == "present":
            begin, stop = first if begin is None else begin, after
        elif begin is not None:
            found += _runs(times, breaks, begin, stop)
            begin = None
    return found + _runs(times, breaks, begin, stop) if begin is not None else found


def _runs(times, breaks, begin, stop):
    """The runs of the samples ``begin`` to ``stop - 1``, cut at the ``breaks`` among them."""
    cuts = [begin, *breaks[bisect_right(breaks, begin) : bisect_left(breaks, stop)], stop]
    return [(times[first], times[after - 1]) for first, after in pairwise(cuts)]


def baseline(samples, quiet_at) -> dict[str, list[int]]:
    """Per-channel sorted values of the radar's quiet samples (``Samples``)."""
    times = samples.times
    quiet = b"".join(
        bytes((bool(quiet_at(times[first])),)) * (after - first)
        for first, after in stretches(len(times), change_points(times, quiet_at))
    )
    values = {}
    for key in samples.keys:
        read = bytes(compress(samples.column(key), quiet)).translate(None, UNREAD)
        if read:
            values[key] = sorted(read)
    return values


def _ceiling(values):
    return values[int((len(values) - 1) * ATTRIBUTION_TAIL_QUANTILE)] + ATTRIBUTION_MARGIN


@lru_cache(maxsize=64)
def _above(ceiling):
    """For each byte, 1 when it is a reading above ``ceiling``."""
    return bytes(int(value <= 100 and value > ceiling) for value in range(256))


def _sustained(rows, key, ceiling):
    """Whether the gate stayed above ``ceiling``, with no recording gap, long enough. A run
    spans last - first + one 6 s cell, so cadence jitter can need one more sample."""
    above = rows.column(key).translate(_above(ceiling))
    gaps = rows.gaps
    for run in ONES.finditer(above):
        first, stop = run.span()
        cuts = [first, *gaps[bisect_right(gaps, first) : bisect_left(gaps, stop)], stop]
        for begin, end in pairwise(cuts):
            if (
                rows.times[end - 1] - rows.times[begin] + SAMPLE_SECONDS
                >= ATTRIBUTION_SUSTAIN_SECONDS
            ):
                return True
    return False


def _human(rows, radar_label_at):
    states = _states(radar_label_at, rows.times)
    for state, answer in (("present", "yes"), ("not_present", "no")):
        if state in states:
            return {"state": answer, "confidence": 1.0}
    return None


def _states(read, times):
    """The answers of ``read`` over the sample ``times`` (in order): one read for each stretch
    between the times it can change (its ``edges``, in order), else one for each sample."""
    edges = getattr(read, "edges", None)
    if edges is None or not len(times):
        return {read(t) for t in times}
    inside = edges[bisect_left(edges, times[0]) : bisect_right(edges, times[-1])]
    return {read(times[first]) for first, _ in stretches(len(times), _cuts(times, inside))}


def _decide(inside, base, radar_label_at):
    """``(result, inferred)`` for the samples ``inside`` an episode: the radar's human label
    first, then its baseline (an inference)."""
    decided = _human(inside, radar_label_at)
    if decided:
        return decided, False
    usable = {key: values for key, values in base.items() if len(values) >= MIN_REFERENCE}
    if not usable:
        return {"state": "unknown", "confidence": 0.0}, True
    seen = [key for key, values in usable.items() if _sustained(inside, key, _ceiling(values))]
    support = max(len(usable[key]) for key in seen or usable)
    return {
        "state": "yes" if seen else "no",
        "confidence": min(1.0, support / ATTRIBUTION_FULL_CONFIDENCE_SAMPLES),
    }, True


def _attributed(found, starts, t):
    """The "yes" attribution of the episode covering ``t``, else None."""
    index = bisect_right(starts, t) - 1
    if index >= 0 and t <= found[index][0][1] and found[index][1]["state"] == "yes":
        return found[index][1]
    return None


def _decider(radar, group_truths, attributions):
    found = {group: list(attributions.get(group, {}).get(radar, ())) for group in group_truths}
    starts = {group: [span[0] for span, _ in items] for group, items in found.items()}

    def decide(t):
        labels = {group: truth_at(t) for group, truth_at in group_truths.items()}
        present = [
            (label, result, group)
            for group, label in labels.items()
            if _state(label) == "present"
            and (result := _attributed(found[group], starts[group], t))
        ]
        if present:
            return "present", present
        if labels and all(_state(label) == "not_present" for label in labels.values()):
            return "not_present", [(label, None, group) for group, label in labels.items()]
        return None

    return decide


def _found(rows, times, truth_at, base, radar_label_at):
    found = []
    for span in episodes(truth_at, times):
        inside = rows[bisect_left(times, span[0]) : bisect_right(times, span[1])]
        result, inferred = _decide(inside, base, radar_label_at)
        # Rule 2 (energy above its own empty baseline) is an inference, not a human label.
        found.append((span, {**result, "inferred": inferred}))
    return found


def quiet_reader(radar_label_at, group_truths):
    """A radar is quiet by its own Empty label or when every one of its groups is Empty."""
    truths = tuple(group_truths.values())

    def quiet(t):
        if radar_label_at(t) == "not_present":
            return True
        for truth_at in truths:
            label = truth_at(t)
            if not label or label[0] != "not_present":
                return False
        return True

    quiet.parts = (radar_label_at, *truths)
    return quiet


def _attributions(radar, samples, radar_label_at, group_truths):
    base = baseline(samples, quiet_reader(radar_label_at, group_truths))
    return {
        group: {radar: _found(samples, samples.times, truth_at, base, radar_label_at)}
        for group, truth_at in group_truths.items()
    }


def weighted_member_target(radar, rows, radar_label_at, group_truths, automatic=None, found=None):
    """Union rule (spec 4.3): a detection target when any group is Present with ``radar``
    attributed; a quiet target only when every group is Empty; otherwise None. ``radar`` is
    attributed over each group's episodes against its own empty baseline (quiet when every
    group is Empty, or by its own Empty label); ``rows`` are its samples in time order.
    Answers ``(state, source, confidence)``. Human truth keeps confidence 1 and wins, except
    human room Present attributed only by rule 2 (the radar's baseline): that is an
    inference, source "attributed" with the attribution confidence (floored like the room
    estimate), or the group's own ``automatic`` room evidence of Present
    (``group -> reader``) when that is more confident. Automatic truth carries its own
    confidence. The learner applies the automatic evidence weight to every non-human answer.
    ``found`` reuses attributions already computed for these truths (``radar_attribution``)."""
    if found is None:
        found = _attributions(radar, rows, radar_label_at, group_truths)
    decide = _decider(radar, group_truths, found)
    automatic = automatic or {}

    def target(t):
        decided = decide(t)
        if decided is None:
            return None
        state, items = decided
        if state != "present":
            return _empty(item[0] for item in items)
        return _present(items, lambda group: automatic[group](t) if group in automatic else None)

    return target


def _present(items, automatic_at):
    human = [(result, group) for label, result, group in items if is_human(label)]
    if any(not result.get("inferred") for result, _ in human):
        return "present", "human", 1.0
    if human:
        return _inferred(human, automatic_at)
    automatic = [label for label, _, _ in items]
    return "present", _automatic_source(automatic), max(x[2] for x in automatic)


def _inferred(human, automatic_at):
    """Human room Present seen only through rule 2: the more confident of the attribution
    guess (below MIN_AUTO_CONFIDENCE it is no guess, as for the estimate) and the room's own
    automatic evidence of Present."""
    guesses = [
        ("present", "attributed", result["confidence"])
        for result, _ in human
        if result["confidence"] >= MIN_AUTO_CONFIDENCE
    ]
    guesses += [
        label
        for _, group in human
        if (label := automatic_at(group)) is not None and label[0] == "present"
    ]
    return max(guesses, key=lambda guess: guess[2], default=None)


def is_human(answer) -> bool:
    """The one human/automatic split of a room answer ``(state, source, confidence)``: human
    room truth or a human target; every other source (sensor labels, the estimate, rule-2
    attribution) is an automatic guess. The learner, references, roles and Seen by decide
    each span with it."""
    return answer is not None and answer[1] == "human"


def stronger(guess, other):
    """The more confident of a room guess and automatic room evidence of the same state;
    ``guess`` keeps a tie or a contrary ``other``. A human ``other`` is never a guess."""
    if other is None or is_human(other):
        return guess
    if guess is None:
        return other
    return other if other[0] == guess[0] and other[2] > guess[2] else guess


def select(room, automatic):
    """Per span, the human-truth target (``room_targets``) where it is human; otherwise the
    more confident of its guess and the automatic target (``auto_targets``). None when both
    are None. The learner and reference training read room evidence through it."""
    if room is None and automatic is None:
        return None

    def read(t):
        target = room(t) if room is not None else None
        if is_human(target):
            return target
        return stronger(target, automatic(t) if automatic is not None else None)

    return read


def _empty(labels):
    labels = list(labels)
    automatic = [x for x in labels if not is_human(x)]
    if not automatic:
        return "not_present", "human", 1.0
    return "not_present", _automatic_source(automatic), min(x[2] for x in automatic)


def _automatic_source(labels):
    # "estimate" only when no room sensor label decided it: references weigh them apart.
    return "estimate" if all(x[1] == "estimate" for x in labels) else "automatic"


def grouped(definitions) -> set[str]:
    """Radars that belong to at least one multi-radar group."""
    return {
        key
        for item in definitions.values()
        if len(item["device_ids"]) > 1
        for key in item["device_ids"]
    }


def _same(value):
    return value


def label_fields(device, copy=_same):
    """The fields of a member radar that decide its labels (``LABEL_KEYS``)."""
    return {key: copy(value) for key, value in device.items() if key in LABEL_KEYS}


def _inputs(runtime, device_id, definitions, copy):
    own = [key for key, item in definitions.items() if device_id in item["device_ids"]]
    related = truth.related_to(definitions, own)
    labels = runtime.data.get("room_labels", {})
    devices = runtime.data["devices"]
    members = {key for group in own for key in definitions[group]["device_ids"]}
    return {
        "device_id": device_id,
        "own": own,
        "groups": copy(definitions),
        "room_labels": {key: copy(labels[key]) for key in related if key in labels},
        "members": {key: label_fields(devices[key], copy) for key in members if key in devices},
    }


def snapshot(runtime, device_id):
    """Copy, on the event loop, the room labels and member labels that decide this radar's
    targets; None when the radar is in no multi-radar group (its evidence is unchanged)."""
    _, _, definitions = groups.definitions(runtime)
    if device_id not in grouped(definitions):
        return None
    return _inputs(runtime, device_id, definitions, deepcopy)


def _snapshot_for(runtime, device_id):
    data = runtime.data
    if "room_snapshot" in data:  # A history view carries its event-loop snapshot.
        saved = data["room_snapshot"]
        return saved if saved and saved["device_id"] == device_id else None
    return snapshot(runtime, device_id) if runtime.hass is not None else None


def _has_room_label(saved, source):
    return any(
        item.get("source") == source for items in saved["room_labels"].values() for item in items
    )


def _labelled(saved):
    """Human room truth needs a manual room label or another member's label."""
    return _has_room_label(saved, "manual") or any(
        device.get("history_labels") or device.get("training_label_start") is not None
        for key, device in saved["members"].items()
        if key != saved["device_id"]
    )


def _reader(context):
    return lambda t: truth.room_truth(context, t)


def _contexts(saved, others=None):
    """Human-only contexts; with ``others`` (every member's guesses) they carry the estimate."""
    inputs = {}
    for key, device in saved["members"].items():
        index = label_index(device)
        inputs[key] = {"manual": index_reader(index), "automatic": NO_GUESSES, "edges": index[0]}
    return {
        group: truth.context(
            saved,
            group,
            {key: inputs[key] for key in saved["groups"][group]["device_ids"] if key in inputs},
            _estimate(saved["groups"][group]["device_ids"], others),
        )
        for group in saved["own"]
    }


def _estimate(ids, others):
    if others is None:
        return None
    read = truth.estimate_reader({key: {"automatic": others[key]} for key in ids if key in others})

    def tagged(timestamp):
        label = read(timestamp)
        return (label[0], "estimate", label[2]) if label else None

    def bulk(times):
        return [(x[0], "estimate", x[2]) if x else None for x in read.bulk(times)]

    tagged.bulk = bulk
    tagged.edges = list(
        chain.from_iterable(
            part for key in ids if key in others for part in (others[key].starts, others[key].ends)
        )
    )
    return tagged


_NAN = object()


def _cuts(times, edges):
    """The index of the first sample at or after each of the ``edges``, once each, in order."""
    found, last = array("I"), -1
    for edge in sorted(edges):
        index = bisect_left(times, edge)
        if index != last:
            found.append(index)
            last = index
    return found


class Memo:
    """``read`` with each answer kept: every sample's truth is evaluated once. The answer at
    a sample of ``times`` (sorted) is kept as a small number naming one of the distinct
    answers: 4 bytes a sample, where a dictionary entry per sample took 100 at 30 days.

    ``edges``, when given, are the times at which ``read`` can change its answer: the samples
    between two of them read alike, so one read answers them all."""

    __slots__ = (
        "read",
        "times",
        "size",
        "codes",
        "cuts",
        "answers",
        "numbers",
        "elsewhere",
        "near",
        "bulk",
    )

    def __init__(self, read, times, edges=None, bulk=None):
        """``bulk``, when given, reads many times at once (``[read(t) for t in ts]``): the first
        read then answers every stretch together."""
        self.read, self.times, self.size, self.bulk = read, times, len(times), bulk
        self.codes = array("I", [0]) * self.size  # 0: not read yet; else 1 + answers index.
        # Where each stretch of samples that read alike begins; None: every sample may differ.
        self.cuts = None if edges is None else _cuts(times, edges)
        self.answers, self.numbers, self.elsewhere = [], {}, {}
        self.near = 0  # Where the last answer was found: reads come mostly in time order.

    def __call__(self, t):
        times, index = self.times, self.near
        if index >= self.size or times[index] != t:
            index += 1
            if index >= self.size or times[index] != t:
                index = bisect_left(times, t)
                if index >= self.size or times[index] != t:
                    return self._other(t)  # Not a sample time: kept as it always was.
            self.near = index
        code = self.codes[index]
        if not code and self.bulk is not None and self.cuts is not None:
            self._fill()
            code = self.codes[index]
        if not code:
            code = self._number(self.read(t))
            self._keep(index, code)
        return self.answers[code - 1]

    def _fill(self):
        """Answer every stretch at once, by the first sample of each."""
        bounds = list(stretches(self.size, self.cuts))
        answers = self.bulk([self.times[first] for first, _ in bounds])
        self.bulk = None
        for (first, after), answer in zip(bounds, answers, strict=True):
            self.codes[first:after] = array("I", [self._number(answer)]) * (after - first)

    def _keep(self, index, code):
        if self.cuts is None:
            self.codes[index] = code
            return
        after = bisect_right(self.cuts, index)
        first = self.cuts[after - 1] if after else 0
        stop = self.cuts[after] if after < len(self.cuts) else self.size
        self.codes[first:stop] = array("I", [code]) * (stop - first)

    def _other(self, t):
        if t not in self.elsewhere:
            self.elsewhere[t] = self.read(t)
        return self.elsewhere[t]

    def _number(self, answer):
        # NaN is not equal to itself: as it is, it would be a new answer on every read.
        key = tuple(_NAN if x != x else x for x in answer) if type(answer) is tuple else answer
        if key not in self.numbers:
            self.answers.append(answer)
            self.numbers[key] = len(self.answers)
        return self.numbers[key]


def _rows(runtime, device_id):
    device = runtime.data["devices"][device_id]
    return Samples.read(runtime._history_columns(device, getattr(runtime, "_attribution_since", 0)))


def _member_guesses(runtime, saved):
    """Every member's recorded guesses, the radar's own included: automatic evidence trains
    at a reduced weight (confidence x the automatic evidence weight) rather than being excluded."""
    devices = runtime.data["devices"]
    return {key: member_guess(runtime, key) for key in saved["members"] if key in devices}


def member_guess(runtime, key):
    """``key``'s guesses as evidence over its retained history (``guesses.evidence``), built
    once for all the views of a job (``prepare``): each would otherwise build every member's,
    once per member."""
    memo = getattr(runtime, "_attribution_guesses", None)
    if memo is None:
        return guesses.evidence(runtime, key)
    if key not in memo:
        memo[key] = guesses.evidence(runtime, key)
    return memo[key]


def radar_attribution(runtime, device_id, saved, human=True, others=None) -> dict:
    """The attribution the learner uses for ``device_id``, shared with the panel's Seen by.

    ``saved`` is the radar's room snapshot (``snapshot``). ``found`` maps each of the radar's
    groups to its time-ordered ``(episode, result)`` list; results carry ``inferred`` (rule 2,
    the radar's baseline, rather than its own label). ``human`` True reads human room truth
    and the human-only baseline (``room_targets``); False reads full room truth: sensor labels
    and the estimate from ``others`` (members' guess cells, read when None; ``auto_targets``).
    Also returns the radar's sample ``times`` (an array of floats; its gate values are not
    kept), ``own`` label reader, per-group ``truths`` and ``contexts``.

    In a job ``prepare``d for it (a reference rebuild or a Seen by build), both answers come
    from one computation (``_both``) and its ``found`` lists from the attribution cache."""
    shared = _shared(runtime, device_id, saved, others)
    if shared is not None:
        kind = "human" if human else "full"
        return {**shared, "truths": shared["truths"][kind], "found": shared["found"][kind]}
    rows = _rows(runtime, device_id)
    times = rows.times
    if human:
        contexts = _contexts(saved)
        truths = {
            group: _memo(_human_only(_reader(item)), item, times)
            for group, item in contexts.items()
        }
    else:
        contexts = _contexts(saved, _member_guesses(runtime, saved) if others is None else others)
        truths = _full_truths(contexts, times)
    own = _history_label_reader(runtime.data["devices"][device_id])
    return {
        "times": times,
        "own": own,
        "truths": truths,
        "contexts": contexts,
        "found": _flat(device_id, rows, own, truths),
    }


def _memo(read, context, times):
    """``read`` over the samples ``times``, answered once for each stretch between the times
    the room truth of ``context`` can change."""
    return Memo(read, times, truth.edges(context))


def _full_truths(contexts, times):
    return {
        group: Memo(
            _reader(context),
            times,
            truth.edges(context),
            lambda ts, context=context: truth.room_truths(context, ts),
        )
        for group, context in contexts.items()
    }


def _flat(device_id, rows, own, truths):
    return {
        group: items[device_id]
        for group, items in _attributions(device_id, rows, own, truths).items()
    }


def _human_only(read, cuts=None):
    """``read`` where its answer is human. ``cuts`` are the sample indices where that can
    change, when that is less often than ``read`` can."""

    def human(t):
        label = read(t)
        return label if is_human(label) else None

    if cuts is None:
        human.parts = (read,)
    else:
        human.cuts = cuts
    return human


def _shared(runtime, device_id, saved, others):
    """The job's one computation for ``device_id``, when the job enabled it, ``saved`` is the
    view's own room snapshot and ``others`` the members' own recorded guesses. Any other
    snapshot (the roles fallback) or guesses compute alone, outside the memo and cache."""
    memo = getattr(runtime, "_attribution_both", None)
    if memo is None or saved is not runtime.data.get("room_snapshot"):
        return None
    if device_id not in memo:
        memo[device_id] = _both(runtime, device_id, saved)
    shared = memo[device_id]
    return shared if others is None or others == shared["others"] else None


def _both(runtime, device_id, saved):
    """Human and full attribution from one room-truth pass. Human room truth is full room
    truth where that is human: levels 1-3 never read guesses here (``NO_GUESSES``), so sensor
    labels and the estimate only answer where human truth has none. A cached result (same
    inputs, ``prepare``) needs no sample rows, so they are decoded only on a miss and dropped
    once attributed."""
    others = _member_guesses(runtime, saved)
    contexts = _contexts(saved, others)
    slot = getattr(runtime, "_attribution_slots", {}).get(device_id, {"found": None})
    rows = _rows(runtime, device_id) if slot["found"] is None else None
    if rows is not None:
        slot["times"] = rows.times
    full = _full_truths(contexts, slot["times"])
    truths = {
        "human": {
            group: _human_only(read, _cuts(slot["times"], truth.edges(contexts[group], False)))
            for group, read in full.items()
        },
        "full": full,
    }
    own = _history_label_reader(runtime.data["devices"][device_id])
    if rows is not None:
        slot["found"] = {kind: _flat(device_id, rows, own, truths[kind]) for kind in truths}
    return {
        "times": slot["times"],
        "own": own,
        "contexts": contexts,
        "others": others,
        "truths": truths,
        "found": slot["found"],
    }


def history_fingerprint(device):
    """Changes on every flush (a new last block) and prune or trim (the first block)."""
    history = device.get("history", [])
    if not history:
        return (0,)
    first, last = history[0], history[-1]
    return (
        len(history),
        first.get("start"),
        first.get("count"),
        last.get("start"),
        last.get("count"),
    )


def member_signature(device):
    return device.get("label_revision", 0), tuple(device.get(field) for field in TRAINING_FIELDS)


def _label_mark(runtime, labels, key):
    items = labels.get(key, [])
    return (
        key,
        runtime.data.get("room_label_revision", {}).get(key, 0),
        runtime.data.get("room_sensor_revision", {}).get(key, 0),
        len(items),
        max((item["end"] for item in items), default=None),
    )


def _history_mark(runtime, key, device):
    pending = recording.pending_samples(runtime, key)
    return key, member_signature(device), history_fingerprint(device), len(pending)


def cache_key(runtime, device_id, saved) -> tuple:
    """What decides a radar's attribution (spec 4.3): its groups' definitions, the related
    room labels (manual and sensor revisions, and their count and last end, which an open
    sensor label extends without a revision), each member's label revision and live
    training, and each member's history (stored block set and pending samples), plus the
    retention window. Samples ageing out at the retention edge change it only once the
    hourly cleanup prunes their block."""
    from ..history.policy import retention_seconds

    own = saved["own"]
    related = truth.related_to(saved["groups"], own)
    devices = runtime.data["devices"]
    return (
        device_id,
        tuple((key, repr(saved["groups"].get(key))) for key in sorted({*own, *related})),
        tuple(_label_mark(runtime, saved["room_labels"], key) for key in related),
        tuple(
            _history_mark(runtime, key, devices[key])
            for key in sorted(saved["members"])
            if key in devices
        ),
        retention_seconds(runtime.data),
    )


def _cached(runtime, device_id, key):
    """``(found, times)`` of the cached attribution of ``device_id`` when it was computed from
    the inputs ``key``, else ``(None, None)``."""
    entry = runtime._attribution_cache.get(device_id)
    return entry[1:] if entry is not None and entry[0] == key else (None, None)


def prepare(runtime, view, device_id, shared=None, since=None) -> None:
    """On the event loop: let the executor job on ``view`` compute both attributions of
    ``device_id`` once, and reuse a cached result whose
    inputs (``cache_key``) are unchanged. ``remember`` keeps what it computed. The views of
    one job pass the same ``shared`` dictionary, in which the members' guesses are built once."""
    view._attribution_since = since or 0
    view._attribution_guesses = {} if shared is None else shared
    view._attribution_both = {}
    view._attribution_slots = {}
    saved = view.data.get("room_snapshot")
    if saved:  # A radar in no multi-radar group reads no room attribution.
        key = (*cache_key(runtime, device_id, saved), since)
        found, times = _cached(runtime, device_id, key)
        view._attribution_slots[device_id] = {"key": key, "found": found, "times": times}


def adopt(runtime, views) -> None:
    """On the event loop, as a job starts: take what jobs that ran since its views were
    prepared have computed for them, where the inputs are the same (``cache_key``). Jobs
    prepared in one poll would otherwise each attribute a radar they share."""
    for view in views:
        for device_id, slot in getattr(view, "_attribution_slots", {}).items():
            if slot["found"] is None:
                slot["found"], slot["times"] = _cached(runtime, device_id, slot["key"])


def prepared_view(runtime, device_id, shared=None, since=None):
    """``device_id``'s history view for an executor job, ``prepare``d for its attribution
    over the history from ``since`` (None: all of it)."""
    view = runtime._history_view(device_id)
    prepare(runtime, view, device_id, shared, since)
    return view


HEAVY = ("others", "contexts", "truths")  # What a job's memo holds besides its result.


def release(views) -> None:
    """At the end of an executor job, let go of what its views hold for attribution (every
    member's guesses and the truth readers): millions of small objects at 30 days, freed
    here, in the executor, instead of stalling the event loop when the views are dropped
    there. A released view is finished: only its attribution result is kept (``remember``)."""
    for view in views:
        getattr(view, "_attribution_guesses", {}).clear()
        for entry in getattr(view, "_attribution_both", {}).values():
            for key in HEAVY:
                entry.pop(key, None)


def remember(runtime, view) -> None:
    """On the event loop, after the job: keep the job's attribution for the next one."""
    for device_id, slot in getattr(view, "_attribution_slots", {}).items():
        if slot["found"] is not None:
            runtime._attribution_cache[device_id] = (slot["key"], slot["found"], slot["times"])
    for key in set(runtime._attribution_cache) - set(runtime.data["devices"]):
        runtime._attribution_cache.pop(key)


def _targets(device_id, learned, own, automatic=False):
    """``weighted_member_target`` over the ``own`` groups of a ``radar_attribution`` result.
    ``automatic``: where human room Present is inferred for this radar (rule 2), the room's
    own sensor labels or estimate still count when they are more confident than the guess."""
    extra = (
        {group: lambda t, c=learned["contexts"][group]: truth._automatic(c, t) for group in own}
        if automatic
        else None
    )
    return weighted_member_target(
        device_id,
        (),  # Unused: ``found`` holds the attribution.
        learned["own"],
        {group: learned["truths"][group] for group in own},
        extra,
        found={group: {device_id: learned["found"][group]} for group in own},
    )


def _automatic_evidence(saved, others):
    return _has_room_label(saved, "sensor") or any(others.values())


def auto_targets(runtime, device_id):
    """Per-radar targets where room truth is human or level-4 automatic (sensor labels, then
    the room estimate over all members), as ``t -> (state, source, confidence)`` with
    source "human", "automatic" (sensor labels) or "estimate".
    None when the radar is in no multi-radar group or no automatic evidence exists."""
    saved = _snapshot_for(runtime, device_id)
    if saved is None:
        return None
    others = _member_guesses(runtime, saved)
    if not _automatic_evidence(saved, others):
        return None
    learned = radar_attribution(runtime, device_id, saved, human=False, others=others)
    return _targets(device_id, learned, saved["own"], automatic=True)


def room_targets(runtime, device_id):
    """Per-radar targets from human room truth (spec 4.3 "Uses") as
    ``t -> (state, source, confidence)``: source "human" (room Empty, or Present the radar's
    own label attributes) or "attributed" (Present inferred from its baseline: an automatic
    guess). None when the radar is in no multi-radar group or no human room truth can exist.
    The radar's own label wins first: callers consult this only where that label is absent."""
    saved = _snapshot_for(runtime, device_id)
    if saved is None or not _labelled(saved):
        return None
    return _targets(device_id, radar_attribution(runtime, device_id, saved), saved["own"])


def group_target(runtime, device_id, saved, group, others=None):
    """The learner's target for ``device_id`` in one of its groups (the joint objective):
    ``room_targets`` and ``auto_targets`` on ``group`` alone, chosen per span by ``select``.
    Attribution is the learner's (``radar_attribution``: one baseline, the union rule)."""
    human = radar_attribution(runtime, device_id, saved, others=others)
    full = radar_attribution(runtime, device_id, saved, human=False, others=others)
    return select(
        _targets(device_id, human, [group]), _targets(device_id, full, [group], automatic=True)
    )


def room_empty_at(labels, t) -> bool:
    """Cheap check: a manual room Empty label in ``labels`` (lists of labels) covers ``t``."""
    return any(
        item.get("source") == "manual"
        and item.get("state") == "not_present"
        and item["start"] <= t < item["end"]
        for items in labels
        for item in items
    )


def membership(definitions, now) -> dict:
    """Group definitions cached for live sampling; refreshed on membership changes."""
    return {"at": now, "definitions": definitions, "grouped": grouped(definitions)}


def _membership(runtime):
    # Area moves in the HA device registry have no event hook here, so the cache also expires.
    now = time.monotonic()
    known = getattr(runtime, "_room_membership", None)
    if known is None or now - known["at"] > MEMBERSHIP_TTL:
        known = membership(groups.definitions(runtime)[2], now)
        runtime._room_membership = known
    return known


def room_quiet(runtime, device_id, t) -> bool:
    """True when every group of a radar in a multi-radar group is human-Empty at ``t``.
    Runs on every live sample: no registry walk or copy unless an Empty room label covers
    ``t``, and then only cached definitions and the radar's own related groups are read."""
    if not room_empty_at(runtime.data.get("room_labels", {}).values(), t):
        return False
    known = _membership(runtime)
    if device_id not in known["grouped"]:
        return False
    saved = _inputs(runtime, device_id, known["definitions"], _same)
    if not room_empty_at(saved["room_labels"].values(), t):
        return False
    return all(
        _state(_human_only(_reader(item))(t)) == "not_present" for item in _contexts(saved).values()
    )

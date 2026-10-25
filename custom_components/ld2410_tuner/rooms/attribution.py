"""Attribution (spec 4.3): which member radars saw each occupied episode of a group."""

from __future__ import annotations

import time
from array import array
from bisect import bisect_left, bisect_right
from contextlib import contextmanager
from copy import deepcopy
from itertools import chain, pairwise
from math import inf, nextafter
from types import SimpleNamespace

from ..calibration.constants import (
    MIN_AUTO_CONFIDENCE,
)
from ..const import HISTORY_KEYS, TRAINING_FIELDS
from ..history import guesses
from ..history.label_lookup import index_reader, label_index
from . import truth

GAP = guesses.GAP
MEMBERSHIP_TTL = 300.0
LABEL_KEYS = ("history_labels", *TRAINING_FIELDS)
# Human room truth never depends on member guesses, so per-radar targets skip them.
NO_GUESSES = guesses.EMPTY


def _state(label):
    return label[0] if label else None


STRIDE = len(HISTORY_KEYS)
MISSING = 255  # What a gate that did not read is kept as.


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
        original = getattr(reader, "times", times)
        if original is times:
            return cuts
        return _rebase_cuts(times, original, cuts)
    edges = getattr(reader, "edges", None)
    if edges is not None:
        return _cuts(times, edges)
    parts = getattr(reader, "parts", None)
    found = None if parts is None else [change_points(times, part) for part in parts]
    return None if found is None or None in found else sorted(set(chain.from_iterable(found)))


def _rebase_cuts(times, original, cuts):
    if not len(times):
        return array("I")
    low, high = bisect_left(original, times[0]), bisect_right(original, times[-1])
    first, after = bisect_left(cuts, low), bisect_right(cuts, high)
    local = cuts[max(0, first - 1) : after + 1]
    return _cuts(times, (original[index] for index in local if index < len(original)))


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


def weighted_member_target(radar, radar_label_at, group_truths, automatic=None, *, found):
    """Union rule (spec 4.3): a detection target when any group is Present with ``radar``
    attributed; a quiet target only when every group is Empty; otherwise None. ``radar`` is
    attributed over each group's episodes against its own empty baseline (quiet when every
    group is Empty, or by its own Empty label).
    Answers ``(state, source, confidence)``. Human truth keeps confidence 1 and wins, except
    human room Present attributed only by rule 2 (the radar's baseline): that is an
    inference, source "attributed" with the attribution confidence (floored like the room
    estimate), or the group's own ``automatic`` room evidence of Present
    (``group -> reader``) when that is more confident. Automatic truth carries its own
    confidence. The learner applies the automatic evidence weight to every non-human answer.
    ``found`` holds the attributions computed for these truths (``radar_attribution``)."""
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

    bounds = [
        point
        for groups_of_radar in found.values()
        for span, _ in groups_of_radar[radar]
        for point in (span[0], nextafter(span[1], inf))
    ]
    target.parts = (*group_truths.values(), *automatic.values(), SimpleNamespace(edges=bounds))
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

    read.parts = tuple(part for part in (room, automatic) if part is not None)
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
    from . import groups

    _, _, definitions = groups.definitions(runtime)
    if device_id not in grouped(definitions):
        return None
    return _inputs(runtime, device_id, definitions, _copier(runtime))


@contextmanager
def shared_copies(runtime):
    """Snapshots taken within share each copied input: a job preparing every radar of a
    room copies each member's labels once, not once per radar (twelve radars: 144 copies)."""
    previous = runtime.__dict__.get("_snapshot_copies")
    runtime._snapshot_copies = {} if previous is None else previous
    try:
        yield
    finally:
        runtime._snapshot_copies = previous


def _copier(runtime):
    memo = runtime.__dict__.get("_snapshot_copies")
    if memo is None:
        return deepcopy

    def copy(value):
        found = memo.get(id(value))
        if found is None or found[0] is not value:  # Keeps the original: its id stays its own.
            found = memo[id(value)] = value, deepcopy(value)
        return found[1]

    return copy


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
    """Room truth at ``t``, with the times its answer can change (``edges``): readers that
    cut physical time into cells need every boundary, not only the samples'."""

    def read(t):
        return truth.room_truth(context, t)

    read.edges = tuple(sorted(set(truth.edges(context))))
    return read


def _contexts(saved, others=None):
    """Human-only contexts; with ``others`` (every member's guesses) they carry the estimate."""
    inputs = {}
    for key, device in saved["members"].items():
        index = label_index(device, groups=False)
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


def _cuts(times, edges):
    """The index of the first sample at or after each of the ``edges``, once each, in order."""
    found, last = array("I"), -1
    for edge in sorted(edges):
        index = bisect_left(times, edge)
        if index != last:
            found.append(index)
            last = index
    return found


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


def radar_attribution(runtime, device_id, saved, human=True, others=None, *, reuse=True) -> dict:
    """The attribution the learner uses for ``device_id``, shared with the panel's Seen by.

    ``saved`` is the radar's room snapshot (``snapshot``). ``found`` maps each of the radar's
    groups to its time-ordered ``(episode, result)`` list; results carry ``inferred`` (rule 2,
    the radar's baseline, rather than its own label). ``human`` True reads human room truth
    and the human-only baseline (``room_targets``); False reads full room truth: sensor labels
    and the estimate from ``others`` (members' guess cells, read when None; ``auto_targets``).
    Also returns the radar's ``own`` label reader, per-group ``truths`` and ``contexts``.

    In a job ``prepare``d for it (a reference rebuild or a Seen by build), both answers come
    from one computation (``_both``) and its ``found`` lists from the attribution cache."""
    from . import event_attribution

    return event_attribution.calculate(runtime, device_id, saved, human, others)


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
        human.times = read.times
    return human


def _source(learned, name):
    value = learned[name]
    return learned["_sources"]()[name] if value is None else value


def history_fingerprint(runtime, key, device):
    """Changes with every record or finished-day change (``recording.history_mark``)."""
    from ..history import recording

    return recording.history_mark(runtime, key)


def member_signature(device):
    return device.get("label_revision", 0), tuple(device.get(field) for field in TRAINING_FIELDS)


def prepare(runtime, view, device_id, shared=None, since=None) -> None:
    """On the event loop: let the executor job on ``view`` attribute ``device_id`` over the
    history from ``since`` (None: all of it). The views of one job pass the same ``shared``
    dictionary, in which the members' guesses are built once."""
    view._attribution_since = since or 0
    view._attribution_guesses = {} if shared is None else shared


def prepared_view(runtime, device_id, shared=None, since=None):
    """``device_id``'s history view for an executor job, ``prepare``d for its attribution
    over the history from ``since`` (None: all of it)."""
    view = runtime._history_view(device_id)
    prepare(runtime, view, device_id, shared, since)
    return view


def release(views) -> None:
    """At the end of an executor job, let go of what its views hold for attribution (every
    member's guesses and the job's attributions): many small objects at 30 days, freed here,
    in the executor, instead of stalling the event loop when the views are dropped there."""
    for view in views:
        getattr(view, "_native_found_job", {}).clear()
        getattr(view, "_attribution_guesses", {}).clear()


def _targets(device_id, learned, own, automatic=False):
    """``weighted_member_target`` over the ``own`` groups of a ``radar_attribution`` result.
    ``automatic``: where human room Present is inferred for this radar (rule 2), the room's
    own sensor labels or estimate still count when they are more confident than the guess."""
    extra = (
        {group: _automatic_reader(_source(learned, "contexts")[group]) for group in own}
        if automatic
        else None
    )
    return weighted_member_target(
        device_id,
        learned["own"],
        {group: learned["truths"][group] for group in own},
        extra,
        found={group: {device_id: learned["found"][group]} for group in own},
    )


def _automatic_reader(context):
    def read(t):
        return truth._automatic(context, t)

    read.edges = list(truth.edges(context))
    return read


def _automatic_evidence(saved, others):
    return _has_room_label(saved, "sensor") or any(others.values())


def auto_targets(runtime, device_id, *, reader=None):
    """Per-radar targets where room truth is human or level-4 automatic (sensor labels, then
    the room estimate over all members), as ``t -> (state, source, confidence)`` with
    source "human", "automatic" (sensor labels) or "estimate".
    None when the radar is in no multi-radar group or no automatic evidence exists."""
    saved = _snapshot_for(runtime, device_id)
    if saved is None:
        return None
    others = (
        _member_guesses(runtime, saved)
        if reader is None
        else {
            key: reader if key == device_id else guesses.of(runtime, key)
            for key in saved["members"]
            if key in runtime.data["devices"]
        }
    )
    if not _automatic_evidence(saved, others):
        return None
    learned = radar_attribution(
        runtime, device_id, saved, human=False, others=others, reuse=reader is None
    )
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
    from . import groups

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

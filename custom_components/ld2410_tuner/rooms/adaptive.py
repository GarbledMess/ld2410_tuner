"""Learner-only room policy, shared once per independent member."""

from ..calibration.adaptive import _clip_labels, earlier_view
from ..calibration.constants import MIN_AUTO_CONFIDENCE
from ..calibration.episodes import Episode, chronological_split
from ..calibration.quality import QUALITY_VERSION, QualityModel, learn
from ..calibration.quality_evidence import collect
from ..calibration.selection import select
from ..history import guesses
from . import attribution, evidence, replay, roles, truth
from .intervals import intersect, seconds


def models(contexts):
    return {
        key: value if isinstance(value, QualityModel) else learn(value)
        for key, value in contexts.items()
    }


def corrected_truth(room, inputs):
    """Unknown sensor ownership keeps its prior; estimates cannot multiply member support."""
    context = truth.for_view(room, inputs)
    sensor = context["sensor"]

    def read(timestamp):
        label = sensor(timestamp)
        return (*label[:2], label[2] * 0.75) if label else None

    context["sensor"] = read
    context["estimate"] = lambda timestamp: evidence.combine(
        [item["automatic"].at(timestamp) for item in inputs.values()]
    )
    return context


class CorrectedReader:
    """Raw admission precedes correction; corrected confidence has no second floor."""

    def __init__(self, model):
        self.model = model
        self.raw = model.context.raw_reader()

    def _label(self, label, timestamp):
        if label is None or not MIN_AUTO_CONFIDENCE <= label[2] <= 1:
            return None
        return (
            label[0],
            label[1],
            self.model.correct(label[2], self.model.context.basis_at(timestamp), label[0]),
        )

    def at(self, timestamp, default=None):
        return self._label(self.raw.at(timestamp), timestamp) or default

    def cells(self, start, end):
        return [(a, b, self._label(label, a)) for a, b, label in self.raw.cells(start, end)]

    def sample_guess(self, timestamp):
        return self._label(self.raw.sample_guess(timestamp), timestamp)


def episode_support(quality, members, state, mass=None):
    """Derived estimates add no independent trial to their contributing members."""
    human, automatic = set(), {}
    for key in members:
        model = quality[key]
        human.update(ep for ep in model.context.episodes if ep.state == state)
        for item in model.context.automatic_episodes:
            if item.episode.state != state or not _visible_episode(item.episode, mass):
                continue
            ep = item.episode
            token = (ep.start, ep.end, ep.state, tuple(sorted(set(item.dependencies))))
            value = model.correct(item.confidence, item.basis, state)
            automatic[token] = max(automatic.get(token, 0), value)
    return len(human), sum(automatic.values())


def _visible_episode(episode, mass):
    if mass is None:
        return True
    low, high = max(episode.start, mass.times[0]), min(episode.end, mass.times[-1])
    return high > low and mass.between(low, high) > 0


def independent_reader(rooms):
    """Only explicit room labels; no member-derived or attributed truth."""
    contexts = [truth.context(room, room.get("group_id"), {}) for room in rooms]

    def read(timestamp):
        values = [
            label[0]
            for context in contexts
            if (label := context["own"](timestamp) or truth._implied(context, timestamp))
            is not None
        ]
        return values[0] if values and len(set(values)) == 1 else "unknown" if values else None

    read.edges = sorted({edge for context in contexts for edge in context["boundaries"]})
    return read


def collect_contexts(groups, start, end, rooms=None):
    """One context per unique member across the complete linked set."""
    rooms = rooms or {}
    members = {key: item for found in groups.values() for key, item in found.items()}
    return {
        key: collect(
            item["view"],
            key,
            item["thresholds"],
            start=start,
            end=end,
            human_reader=independent_reader(
                [rooms[gid] for gid, found in groups.items() if key in found and gid in rooms]
            ),
        )
        for key, item in members.items()
    }


def target(members, room, key, quality):
    """Rebuild attribution from raw independent readers, bypassing display caches."""
    saved, group, _ = roles._inputs(members, room, key)
    view = members[key]["view"]
    others = {
        name: quality[name].context.raw_reader() if name in quality else guesses.of(view, name)
        for name in saved["members"]
    }
    human = attribution.radar_attribution(view, key, saved, others=others, reuse=False)
    full = attribution.radar_attribution(view, key, saved, human=False, others=others, reuse=False)
    return attribution.select(
        attribution._targets(key, human, [group]),
        attribution._targets(key, full, [group], automatic=True),
    )


def human_episodes(members, start, end, room, owner):
    """Original explicit room union truth on physical shared recording coverage."""
    inputs = {
        key: evidence.read(
            item["view"],
            key,
            item["thresholds"],
            start,
            end,
            automatic_reader=guesses.EMPTY,
            timing=item["timing"],
        )
        for key, item in members.items()
    }
    context = truth.for_view(room, inputs)
    physical = [
        replay.radar(
            inputs[key]["rows"], item["thresholds"], item["timing"], inputs[key]["barriers"]
        )
        for key, item in members.items()
    ]
    coverage = replay.shared(item["coverage"] for item in physical)
    boundaries = sorted(
        edge
        for edge in {
            start,
            end,
            *context["boundaries"],
            *(edge for item in inputs.values() for edge in item["boundaries"]),
            *(edge for span in coverage for edge in span),
        }
        if start <= edge <= end
    )
    result = []
    for low, high in zip(boundaries, boundaries[1:], strict=False):
        if not intersect(coverage, [(low, high)]):
            continue
        label = truth.room_truth(context, (low + high) / 2)
        if label is not None and label[1] == "human":
            _append_episode(result, low, high, label[0], owner)
    return tuple(result)


def _append_episode(items, low, high, state, owner):
    if items and items[-1].end == low and items[-1].state == state:
        low = items.pop().start
    items.append(Episode(low, high, state, (owner,)))


def _earlier(groups, rooms, cutoff):
    shared = {}
    for found in groups.values():
        for key, item in found.items():
            shared.setdefault(key, {**item, "view": earlier_view(item["view"], cutoff)})
    clipped = {
        gid: {
            **room,
            "room_labels": {
                key: _clip_labels(labels, cutoff)
                for key, labels in room.get("room_labels", {}).items()
            },
        }
        for gid, room in rooms.items()
    }
    return {gid: {key: shared[key] for key in found} for gid, found in groups.items()}, clipped


def _held_score(groups, thresholds, episodes, start, end):
    score = dict.fromkeys(
        ("missed_seconds", "false_seconds", "present_seconds", "empty_seconds"), 0.0
    )
    score["missed_episodes"] = 0
    empty = []
    for gid, found in groups.items():
        physical = [
            replay.radar(
                (
                    inputs := evidence.read(
                        item["view"], key, item["thresholds"], start, end, timing=item["timing"]
                    )
                )["rows"],
                thresholds[key],
                item["timing"],
                inputs["barriers"],
            )
            for key, item in found.items()
        ]
        projected = replay.combined(dict(enumerate(physical)))
        for episode in episodes[gid]:
            side = "lower" if episode.state == "present" else "upper"
            hit = seconds(intersect(projected[side], [(episode.start, episode.end)]))
            duration = episode.end - episode.start
            if side == "lower":
                score["present_seconds"] += duration
                score["missed_seconds"] += duration - hit
                score["missed_episodes"] += hit == 0
            else:
                score["empty_seconds"] += duration
                score["false_seconds"] += hit
                empty.append(hit)
    return score, empty


def choose_policy(groups, start, end, rooms, weight, contexts):
    """Exactly two earlier linked searches and one selected final refit in fitting."""
    selection = {
        "policy": "corrected_positive",
        "validated": False,
        "reason": "no_valid_chronological_cut",
        "cutoff": None,
        "candidates": {},
        "quality_version": QUALITY_VERSION,
        "provenance": "recorded_basis_only",
    }
    audit = {
        "quality": {key: model.audit() for key, model in models(contexts).items()},
        "selection": selection,
        "sensor_provenance": "recorded_owner_unavailable_prior_only",
        "derived_estimate_episode_mass": 0,
    }
    episodes = {
        gid: human_episodes(found, start, end, rooms.get(gid), gid) for gid, found in groups.items()
    }
    independent = [ep for model in models(contexts).values() for ep in model.context.episodes]
    cutoff = chronological_split(
        {*independent, *(ep for items in episodes.values() for ep in items)}
    )
    if cutoff is None:
        return selection["policy"], audit
    return _validated_policy(groups, start, end, rooms, weight, cutoff, episodes, audit)


def _validated_policy(groups, start, end, rooms, weight, cutoff, episodes, audit):
    from .fitting import search
    from .objective import LinkedObjective

    training, clipped = _earlier(groups, rooms, cutoff)
    quality = models(collect_contexts(training, start, cutoff, clipped))
    training = _candidate_sources(training, start, cutoff, quality)
    held = {gid: [ep for ep in items if ep.start >= cutoff] for gid, items in episodes.items()}
    reports, errors = {}, []
    for policy in ("reference", "corrected_positive"):
        objective = LinkedObjective(
            training,
            start,
            cutoff,
            clipped,
            weight,
            quality=quality,
            reference=policy == "reference",
        )
        thresholds = search(objective)
        score, empty = _held_score(groups, thresholds, held, cutoff, end)
        reports[policy] = {"thresholds": thresholds, "score": score}
        if policy == "reference":
            audit["selection"]["reference_sources"] = {
                gid: dict(zip(evidence.LABELS, item.sources, strict=True))
                for gid, item in objective.groups.items()
            }
        errors.append(empty)
        del objective
    ref, pos = [item["score"] for item in reports.values()]
    decision = select(
        ref,
        pos,
        zip(*errors, strict=True),
        measurable=ref["present_seconds"] > 0 and ref["empty_seconds"] > 0,
    )
    audit["selection"].update(
        policy=decision.policy,
        validated=decision.validated,
        reason=decision.reason,
        cutoff=cutoff,
        candidates=reports,
        training_quality={key: model.audit() for key, model in models(quality).items()},
    )
    return decision.policy, audit


def _candidate_sources(groups, start, end, quality):
    """Both policies share immutable packed energies and compatible timing/mask tables."""
    from .objective import Radar

    shared = {}
    for found in groups.values():
        for key, item in found.items():
            if key in shared:
                continue
            inputs = evidence.read(
                item["view"],
                key,
                item["thresholds"],
                start,
                end,
                timing=item["timing"],
                automatic_reader=CorrectedReader(quality[key]),
            )
            shared[key] = {
                **item,
                "adaptive_inputs": inputs,
                "adaptive_radar": Radar(item, inputs, quality[key]),
            }
    return {gid: {key: shared[key] for key in found} for gid, found in groups.items()}

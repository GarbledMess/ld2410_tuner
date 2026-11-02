"""Learner-only room evidence, shared once per independent member.

Quality contexts, corrected automatic readers and the training-only view for the adaptive
policy; the two candidate fits, held-out scoring and choice run in the hybrid facade."""

from ..calibration.adaptive import _clip_labels, earlier_view
from ..calibration.constants import MIN_AUTO_CONFIDENCE
from ..calibration.quality import QualityModel, learn
from ..calibration.quality_evidence import collect
from ..history import guesses
from . import attribution, evidence, roles, truth


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

"""Exact interval-time objective shared with room assessment, with cached radar replays."""

from functools import lru_cache
from hashlib import blake2b

from ..calibration.duration import DurationGroup
from ..calibration.intervals import WeightedTime, across
from ..calibration.metrics import threshold_masks
from ..calibration.scoring import automatic_share, blended_cost, error_cost
from ..calibration.separation import gate_preference
from ..calibration.settings import DEFAULT_AUTOMATIC_WEIGHT
from . import attribution, evidence, replay, roles, truth
from .intervals import Timeline

# Projections of a radar kept for reuse: the other radars do not change while one is searched.
RECENT = 4
NAMED = 1 << 16  # Hit masks whose names are kept by thresholds.
SOURCES = ("human", "automatic")


def fingerprint(bits):
    """A short name for a hit mask: equal masks share it, and no memory holds the mask itself."""
    return blake2b(bits.to_bytes((bits.bit_length() + 7) // 8, "little"), digest_size=16).digest()


class Radar:
    def __init__(self, item, inputs):
        rows, barriers = inputs["rows"], inputs["barriers"]
        self.current = item["thresholds"]
        self.tables = {key: threshold_masks(rows, key) for key in self.current}
        self.positive = DurationGroup(rows, item["timing"], barriers=barriers, measured=False)
        self.negative = DurationGroup(
            rows, item["timing"], absent=True, barriers=barriers, measured=False
        )
        self.projector = replay.Projector(self.positive, self.negative, ("lower", "upper"))
        self.project = lru_cache(maxsize=RECENT)(self.projector.project)
        self.named = {}

    def bits(self, thresholds):
        result = 0
        for key, value in thresholds.items():
            result |= self.tables[key][value]
        return result

    def name(self, thresholds):
        """The ``fingerprint`` of the hit mask ``thresholds`` give, kept for the thresholds: a
        search asks about the same few thousand again and again."""
        key = tuple(thresholds.items())
        found = self.named.get(key)
        if found is None:
            if len(self.named) > NAMED:
                self.named.clear()
            found = self.named[key] = fingerprint(self.bits(thresholds))
        return found


def room_mass(context, coverage, start, end):
    boundaries = {start, end, *(point for span in coverage for point in span)}
    boundaries.update(t for t in context["boundaries"] if start <= t <= end)
    for item in context["members"].values():
        boundaries.update(item["boundaries"])
    times = sorted(boundaries)
    available = Timeline(coverage)
    labels = [
        truth.room_truth(context, (a + b) / 2) if available.at((a + b) / 2) else None
        for a, b in zip(times, times[1:], strict=False)
    ]
    sampled = [*times[:-1], end]  # The four masses share their times.
    return {
        (source, state): WeightedTime(
            sampled,
            [
                *(label[2] if label and label[:2] == (state, source) else 0 for label in labels),
                0,
            ],
        )
        for source in SOURCES
        for state in evidence.LABELS
    }


class Objective:
    def __init__(
        self, members, start, end, room=None, shared=None, automatic_weight=DEFAULT_AUTOMATIC_WEIGHT
    ):
        """``shared`` ({device_id: (inputs, Radar)}) lets linked groups read and replay a
        radar once; room truth and preferences stay per group. Attribution is the learner's
        (``roles.target``): one baseline per radar under the union rule over full history.
        ``automatic_weight`` blends automatic room truth into the ranked cost, as in the
        device learner (``scoring.automatic_share``)."""
        self.start, self.end = start, end
        owner = shared is None  # A linked group leaves the samples to its set.
        shared = {} if shared is None else shared
        for key, item in members.items():
            if key not in shared:
                inputs = evidence.read(item["view"], key, item["thresholds"], start, end)
                shared[key] = inputs, Radar(item, inputs)
        self.inputs = {key: shared[key][0] for key in members}
        self.truth = truth.for_view(room, self.inputs)
        self.radars = {key: shared[key][1] for key in members}
        self.current = {key: dict(item["thresholds"]) for key, item in members.items()}
        coverage = replay.shared(radar.projector.coverage for radar in self.radars.values())
        self.known = {}  # Rates by the hit masks that gave them.
        self.mass = room_mass(self.truth, coverage, start, end)
        self.totals = {key: value.between(start, end) for key, value in self.mass.items()}
        self.counted = tuple(  # The sources with mass in each class.
            [source for source in SOURCES if self.totals[source, label]]
            for label in evidence.LABELS
        )
        # Each class is costed on its own best source: human where it has human mass, else
        # the room's automatic truth (already confidence-weighted). Human classes rank first
        # (spec 4.2); in a room with none, automatic is the primary and has no blend to add.
        self.sources = tuple(
            "human" if self.totals["human", label] else "automatic" for label in evidence.LABELS
        )
        self.automatic_room = "human" not in self.sources
        self.shares = tuple(
            0
            if self.automatic_room
            else automatic_share(
                automatic_weight, self.totals["human", label], self.totals["automatic", label]
            )
            for label in evidence.LABELS
        )
        self.preferences = {
            key: self._preferences(key, roles.target(members, room, key)) for key in members
        }
        self.preferred = {
            device: {key: value["preferred_threshold"] for key, value in gates.items()}
            for device, gates in self.preferences.items()
        }
        if owner:
            self.drop_rows()

    def drop_rows(self):
        """Let go of the samples once the preferences are made: a search reads hit masks."""
        for inputs in self.inputs.values():
            inputs["rows"] = ()

    def _preferences(self, device_id, target):
        """Gate preferences from room truth as this radar's evidence; ``target`` is the
        learner's own target for it in this group (``roles.target``)."""
        groups = {source: {label: [] for label in evidence.LABELS} for source in SOURCES}
        rows = self.inputs[device_id]["rows"]
        labels = dict(zip(rows.times, truth.room_truths(self.truth, rows.times), strict=True))
        for row in rows:
            label = _member_label(labels[row[0]], target, row[0])
            if label:
                state, source, confidence = label
                groups[source][state].append((row[0], row[1], state, confidence))
        return {
            key: gate_preference(key, groups["human"], groups["automatic"], value)
            for key, value in self.current[device_id].items()
        }

    def rates(self, thresholds):
        """``{source: (missed, false-active)}``: the same for thresholds that give the radars
        the same hit masks, so it is worked out once for them."""
        name = tuple(radar.name(thresholds[key]) for key, radar in self.radars.items())
        if name not in self.known:
            masks = [radar.bits(thresholds[key]) for key, radar in self.radars.items()]
            self.known[name] = self._rates(masks)
        return self.known[name]

    def _rates(self, masks):
        projected = [
            radar.project(mask) for radar, mask in zip(self.radars.values(), masks, strict=True)
        ]
        detected = replay.united([item["lower"] for item in projected])
        false = replay.united([item["upper"] for item in projected])
        rates = {source: [0, 0] for source in SOURCES}
        for index, (label, spans) in enumerate((("present", detected), ("not_present", false))):
            counted = self.counted[index]
            if counted:
                masses = [self.mass[source, label] for source in counted]
                for source, total in zip(counted, across(masses, *spans), strict=True):
                    share = total / self.totals[source, label]
                    rates[source][index] = 1 - share if index == 0 else share
        return {source: tuple(pair) for source, pair in rates.items()}

    def costs(self, thresholds):
        rates = self.rates(thresholds)
        return sum(self.parts(rates)), error_cost(*rates["automatic"])

    def parts(self, rates):
        """Error cost of the human-sourced classes, then of the automatic-sourced ones."""
        return tuple(
            error_cost(
                *(
                    rates[source][index] if source == wanted else 0
                    for index, source in enumerate(self.sources)
                )
            )
            for wanted in ("human", "automatic")
        )

    def cost(self, rates):
        """``(blended, cost, derived, automatic)`` for ``rates``, unrounded. Human classes rank
        first (spec 4.2); automatic-sourced classes never overrule them and break its ties
        through ``derived``, then the full ``automatic`` cost. A fully automatic room has no
        human class, so its automatic-sourced cost is the primary."""
        human, derived = self.parts(rates)
        automatic = error_cost(*rates["automatic"])
        cost = derived if self.automatic_room else human
        return blended_cost(cost, self.shares, rates["automatic"]), cost, derived, automatic

    def tail(self, thresholds):
        """The entries of a rank that the thresholds alone decide: ``(supported margin,
        changes, margin)``. Human cost stays the first tie-break under the blend, as in the
        device search."""
        changed = [
            (device, key, value)
            for device, gates in thresholds.items()
            for key, value in gates.items()
            if value != self.current[device][key]
        ]
        margin = sum(abs(value - self.preferred[device][key]) for device, key, value in changed)
        supported_margin = sum(
            abs(value - self.preferred[device][key])
            for device, gates in thresholds.items()
            for key, value in gates.items()
            if supported(self.preferences[device][key])
        )
        return supported_margin, len(changed), margin

    def rank(self, thresholds):
        costs = self.cost(self.rates(thresholds))
        return (*(round(value, 9) for value in costs), *self.tail(thresholds))

    def alike(self, low, high):
        """Whether ``low`` and ``high`` give the same rates, so any thresholds between them (a
        radar hit at least as much as ``high`` and at most as much as ``low``) give them too."""
        return self.rates(low) == self.rates(high)

    def bound(self, low, high):
        """No more than the costs of the rank (unrounded) of any thresholds that hit at least
        what ``low`` hits and at most what ``high`` hits (None: no limit): a radar hit more
        detects at least as much time and is false-active at least as much, so what ``low``
        misses and what ``high`` has false are the least there can be."""
        floor = dict.fromkeys(SOURCES, (0.0, 0.0))
        below = self.rates(low) if low else floor
        above = self.rates(high) if high else floor
        return self.cost({s: (below[s][0], above[s][1]) for s in SOURCES})


class LinkedObjective:
    """Groups sharing radars, fitted together (spec 5): one ``Objective`` per group on its own
    room truth (attribution is the learner's), over the union of member radars, so a shared
    radar has one value. Costs and ranks are per-group sums; one group is its ``Objective``."""

    def __init__(self, groups, start, end, rooms=None, automatic_weight=DEFAULT_AUTOMATIC_WEIGHT):
        rooms = rooms or {}
        shared = {}
        self.groups = {
            gid: Objective(groups[gid], start, end, rooms.get(gid), shared, automatic_weight)
            for gid in sorted(groups)
        }
        for objective in self.groups.values():
            objective.drop_rows()
        self.radars, self.current = {}, {}
        for objective in self.groups.values():
            for key, radar in objective.radars.items():
                self.radars.setdefault(key, radar)
                self.current.setdefault(key, objective.current[key])
        self.preferred = {
            device: {key: self._preferred(device, key) for key in gates}
            for device, gates in self.current.items()
        }

    def _preferred(self, device, key):
        """Lower median of the groups' preferences: for an even count, the lower middle one."""
        values = sorted(
            item.preferred[device][key] for item in self.groups.values() if device in item.radars
        )
        return values[(len(values) - 1) // 2]

    def parts(self, thresholds):
        """Each group's objective with ``thresholds`` restricted to its own radars."""
        for gid, objective in self.groups.items():
            yield gid, objective, {key: thresholds[key] for key in objective.radars}

    def costs(self, thresholds):
        totals = zip(*(item.costs(part) for _, item, part in self.parts(thresholds)), strict=True)
        return tuple(sum(values) for values in totals)

    def tail(self, thresholds):
        return tuple(
            sum(values)
            for values in zip(
                *(item.tail(part) for _, item, part in self.parts(thresholds)), strict=True
            )
        )

    def rank(self, thresholds):
        ranks = [item.rank(part) for _, item, part in self.parts(thresholds)]
        *costs, supported, changed, margin = (sum(v) for v in zip(*ranks, strict=True))
        return (*(round(value, 9) for value in costs), supported, changed, margin)

    def alike(self, low, high):
        return all(
            item.alike({key: low[key] for key in part}, {key: high[key] for key in part})
            for _, item, part in self.parts(low)
        )

    def bound(self, low, high):
        """The least costs between ``low`` and ``high`` (see ``Objective.bound``): the sums
        over groups, each on its own radars."""
        return tuple(
            map(
                sum,
                zip(
                    *(
                        item.bound(
                            low and {key: low[key] for key in part},
                            high and {key: high[key] for key in part},
                        )
                        for _, item, part in self.parts(low or high or self.current)
                    ),
                    strict=True,
                ),
            )
        )


def _member_label(label, target, t):
    """Room truth as this radar's preference evidence. Present counts only while the radar is
    attributed (spec 4.3); human Present attributed by its baseline alone (rule 2) is an
    inference, so it is automatic evidence at the confidence of the guess that decided it
    (the attribution, or more confident automatic room evidence)."""
    if not label or label[0] != "present":
        return label
    decided = target(t)
    if not decided or decided[0] != "present":
        return None
    if attribution.is_human(label) and not attribution.is_human(decided):
        return "present", "automatic", decided[2]
    return label


def supported(preference):
    return (
        preference["separated"]
        and preference["noise_ceiling"]
        < preference["preferred_threshold"]
        < preference["presence_reference"]
    )

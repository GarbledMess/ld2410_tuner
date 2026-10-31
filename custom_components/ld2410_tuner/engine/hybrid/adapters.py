"""Production evidence contracts, deliberately independent of search internals."""

from dataclasses import replace
from math import inf, isfinite

from ...calibration.adaptive import earlier_view
from ...calibration.constants import MIN_AUTO_CONFIDENCE
from ...calibration.thresholds import allowed
from ...const import HISTORY_KEYS
from ...history import daystore, guesses
from ...history.fit_days import FittingDays
from ...history.fit_rows import CapturedRowSource
from ...history.label_lookup import _history_label_reader
from ...rooms import adaptive, attribution, evidence, truth
from .digest import fold_day
from .domains import normalize_pattern
from .quality import collect_quality, learn_quality
from .types import EvidencePolicy, FitProblem, RadarSpec, TruthSpan, ZoneSpec


def _snapshot(view, end):
    result = earlier_view(view, end)
    result._history_until = min(end, getattr(view, "_history_until", end))
    result._attribution_until = min(end, getattr(view, "_attribution_until", end))
    return result


class _WindowRows:
    def __init__(self, source, start, end):
        self.source, self.end = source, end
        self.start = max(start, getattr(source.view, "_physical_history_start", -inf))

    def blocks(self, lo, hi):
        yield from self.source.blocks(max(lo, self.start), min(hi, self.end))

    def rows_in(self, lo, hi):
        return self.source.rows_in(max(lo, self.start), min(hi, self.end))


class _Days:
    def __init__(self, views, start=-inf, end=inf):
        self.views, self.start, self.end = views, start, end

    def _bounds(self, device_id):
        view = self.views[device_id]
        return (
            max(self.start, getattr(view, "_physical_history_start", -inf)),
            min(
                self.end,
                getattr(view, "_history_until", inf),
                getattr(view, "_attribution_until", inf),
            ),
        )

    def dates(self, device_id):
        low, high = self._bounds(device_id)
        for date in FittingDays(self.views[device_id]).dates(device_id):
            start, end = daystore.bounds(date)
            if start < high and end > low:
                yield date

    def digest(self, device_id, date, channels):
        low, high = self._bounds(device_id)
        start, end = daystore.bounds(date)
        view = self.views[device_id]
        if low <= start and end <= high:
            return FittingDays(view).digest(device_id, date, channels)
        if min(end, high) <= max(start, low):
            return None
        rows = CapturedRowSource(view, device_id, channels)
        return fold_day(rows.blocks(max(start, low), min(end, high)), start, end, channels)


def _radar(device_id, keys, current, timing, constraints):
    channels = tuple(HISTORY_KEYS.index(key) for key in keys)
    domains = {
        channel: allowed((constraints or {}).get(key))
        for channel, key in zip(channels, keys, strict=True)
    }
    values = {channel: current[key] for channel, key in zip(channels, keys, strict=True)}
    spec = RadarSpec(device_id, channels, domains, values, dict(timing))
    normalized = normalize_pattern({device_id: spec})[device_id]
    return replace(spec, current=normalized)


def _edges(reader):
    if reader is None:
        return set()
    return set(getattr(reader, "edges", ())).union(
        *(_edges(part) for part in getattr(reader, "parts", ()))
    )


def _independent(view, device_id):
    saved = attribution._snapshot_for(view, device_id)
    if saved is None:
        return None
    return adaptive.independent_reader([{**saved, "group_id": group} for group in saved["own"]])


class _DeviceTargets:
    def __init__(self, view, device_id, model, start, end, auto_start):
        self.model, self.auto_start, self.device_id = model, auto_start, device_id
        self.own = _history_label_reader(view.data["devices"][device_id])
        self.raw = model.context.raw_reader()
        self.room = evidence.streamed_room_targets(view, device_id, self.raw, start, end)
        self.edges = _edges(self.own) | _edges(self.room)
        self.edges.update(point for run in model.context.raw_runs for point in run[:2])

    def answer(self, timestamp):
        own = self.own(timestamp)
        if own is not None:
            return (own, "human", 1.0), ()
        room = self.room(timestamp) if self.room else None
        if attribution.is_human(room):
            return room, ()
        if timestamp < self.auto_start:
            return None, ()
        label = room or self.raw.at(timestamp)
        if label is None or not MIN_AUTO_CONFIDENCE <= label[2] <= 1:
            return None, ()
        basis = guesses.UNKNOWN if room else self.model.context.basis_at(timestamp)
        confidence = self.model.correct(label[2], basis, label[0])
        dependencies = self.room.dependencies_at(timestamp, label[0]) if room else (self.device_id,)
        return (label[0], "automatic", confidence), dependencies


def _spans(reader, edges, start, end, owner):
    cuts = sorted({start, end, *(edge for edge in edges if start < edge < end)})
    result = []
    for low, high in zip(cuts, cuts[1:], strict=False):
        label, dependencies = reader(low)
        if label is None or label[0] not in evidence.LABELS:
            continue
        _append_span(result, low, high, label, owner, dependencies)
    return tuple(result)


def _append_span(result, low, high, label, owner, dependencies):
    state, source, confidence = label
    episode = f"{owner}:{source}:{state}:{low!r}"
    if result and result[-1].end == low:
        last = result[-1]
        if (last.state, last.source) == (state, source):
            episode = last.episode_id
        if (last.state, last.source, last.confidence, last.dependencies) == (*label, dependencies):
            result[-1] = replace(last, end=high)
            return
    result.append(TruthSpan(low, high, state, source, confidence, episode, dependencies))


def _policy(models, weight, reference, zones=()):
    if not isfinite(weight) or not 0 <= weight <= 1:
        raise ValueError("Automatic weight must be between zero and one")
    quality = {"models": models, "audits": {key: model.audit() for key, model in models.items()}}
    quality["zones"] = {zone.group_id: _support(models, zone.members) for zone in zones}
    if len(zones) == 1:
        quality.update(quality["zones"][zones[0].group_id])
    return EvidencePolicy(weight, reference, quality)


def _support(models, members):
    counts = {state: adaptive.episode_support(models, members, state) for state in evidence.LABELS}
    return {
        "human_episodes": {state: pair[0] for state, pair in counts.items()},
        "automatic_episode_mass": {state: pair[1] for state, pair in counts.items()},
    }


def device_problem(
    view,
    device_id,
    keys,
    current,
    timing,
    weight,
    constraints,
    start,
    end,
    *,
    quality_context=None,
    reference=False,
):
    """Own human labels, independent room targets, then admitted corrected guesses."""
    view = _snapshot(view, end)
    end = min(end, view._history_until, view._attribution_until)
    radar = _radar(device_id, keys, current, timing, constraints)
    context = _context_window(quality_context, start, end) or collect_quality(
        view, device_id, keys, start, end, _independent(view, device_id), None
    )
    model = learn_quality(context)
    initial = EvidencePolicy(weight, reference, {})
    auto_start = (
        start
        if initial.automatic_horizon_days is None
        else max(start, end - initial.automatic_horizon_days * 86400)
    )
    targets = _DeviceTargets(view, device_id, model, start, end, auto_start)
    labels = _spans(targets.answer, {*targets.edges, auto_start}, start, end, device_id)
    zones = (ZoneSpec(device_id, (device_id,), labels),)
    rows = CapturedRowSource(view, device_id, radar.channels)
    policy = _policy({device_id: model}, weight, reference, zones)
    return FitProblem(
        {device_id: radar},
        zones,
        start,
        end,
        _Days({device_id: view}, start - 1800, end),
        {device_id: _WindowRows(rows, start - 1800, end)},
        policy,
    )


def _members(groups, end):
    members = {}
    for found in groups.values():
        for key, item in found.items():
            if key not in members:
                members[key] = {**item, "view": _snapshot(item["view"], end)}
    return members


def _room_models(groups, members, rooms, start, end, contexts):
    return {
        key: learn_quality(
            _context_window((contexts or {}).get(key), start, end)
            or collect_quality(
                item["view"],
                key,
                list(item["thresholds"]),
                start,
                end,
                adaptive.independent_reader(
                    [rooms[gid] for gid, found in groups.items() if key in found and gid in rooms]
                ),
                None,
            )
        )
        for key, item in members.items()
    }


def _context_window(context, start, end):
    original = getattr(context, "context", context)
    if original is None or (original.start, original.end) != (start, end):
        return None
    return context


def _room_zone(group, found, rooms, inputs, start, end):
    selected = {key: inputs[key] for key in found}
    context = adaptive.corrected_truth(rooms.get(group), selected)
    edges = {*truth.edges(context), *(edge for item in selected.values() for edge in item["edges"])}

    def read(timestamp):
        label = truth.room_truth(context, timestamp)
        deps = (
            tuple(selected)
            if label and label[1] != "human" and context["sensor"](timestamp) is None
            else ()
        )
        return label, deps

    return ZoneSpec(group, tuple(found), _spans(read, edges, start, end, group))


def room_problem(groups, start, end, rooms, weight, *, quality_contexts=None, reference=False):
    """One source per unique member; each zone retains its own ordered truth."""
    members, rooms = _members(groups, end), rooms or {}
    end = min(
        [end]
        + [
            min(item["view"]._history_until, item["view"]._attribution_until)
            for item in members.values()
        ]
    )
    models = _room_models(groups, members, rooms, start, end, quality_contexts)
    inputs = {
        key: evidence.label_inputs(item["view"], key, adaptive.CorrectedReader(models[key]))
        for key, item in members.items()
    }
    radars = {
        key: _radar(
            key,
            list(item["thresholds"]),
            item["thresholds"],
            item["timing"],
            item.get("constraints"),
        )
        for key, item in members.items()
    }
    zones = tuple(
        _room_zone(group, found, rooms, inputs, start, end) for group, found in groups.items()
    )
    rows = {
        key: _WindowRows(
            CapturedRowSource(item["view"], key, radars[key].channels), start - 1800, end
        )
        for key, item in members.items()
    }
    views = {key: item["view"] for key, item in members.items()}
    return FitProblem(
        radars,
        zones,
        start,
        end,
        _Days(views, start - 1800, end),
        rows,
        _policy(models, weight, reference, zones),
    )

"""Exact interval-time objective shared with room assessment, with cached radar replays."""

from functools import lru_cache

from ..calibration.duration import DurationGroup
from ..calibration.intervals import WeightedTime
from ..calibration.metrics import threshold_masks
from ..calibration.scoring import error_cost
from ..calibration.separation import gate_preference
from . import evidence, replay
from .intervals import Timeline, union


class Radar:
    def __init__(self, item, inputs):
        self.current = item["thresholds"]
        self.rows = inputs["rows"]
        self.tables = {key: threshold_masks(self.rows, key) for key in self.current}
        self.positive = DurationGroup(self.rows, item["timing"], barriers=inputs["barriers"])
        self.negative = DurationGroup(
            self.rows, item["timing"], absent=True, barriers=inputs["barriers"]
        )
        self.project = lru_cache(maxsize=128)(self._project)

    def _project(self, bits):
        return replay.from_bits(self.positive, self.negative, bits)

    def bits(self, thresholds):
        result = 0
        for key, value in thresholds.items():
            result |= self.tables[key][value]
        return result


def truth(inputs, coverage, start, end):
    boundaries = {start, end, *(point for span in coverage for point in span)}
    for item in inputs.values():
        boundaries.update(item["boundaries"])
    times = sorted(boundaries)
    available = Timeline(coverage)
    rows = {(source, label): [] for source in ("human", "automatic") for label in evidence.LABELS}
    for a, b in zip(times, times[1:], strict=False):
        label = evidence.room_label(inputs, (a + b) / 2) if available.at((a + b) / 2) else None
        _truth_row(rows, a, label)
    for samples in rows.values():
        samples.append((end, {}, "unknown", 0))
    return {
        key: WeightedTime([row[0] for row in samples], [row[3] for row in samples])
        for key, samples in rows.items()
    }


class Objective:
    def __init__(self, members, start, end):
        self.start, self.end = start, end
        self.inputs = {
            key: evidence.read(item["view"], key, item["thresholds"], start, end)
            for key, item in members.items()
        }
        self.radars = {key: Radar(item, self.inputs[key]) for key, item in members.items()}
        self.current = {key: dict(item["thresholds"]) for key, item in members.items()}
        projected = {
            key: radar.project(radar.bits(self.current[key])) for key, radar in self.radars.items()
        }
        coverage = replay.combined(projected)["coverage"]
        self.mass = truth(self.inputs, coverage, start, end)
        self.totals = {key: value.between(start, end) for key, value in self.mass.items()}
        self.preferences = {key: self._preferences(key) for key in members}
        self.preferred = {
            device: {key: value["preferred_threshold"] for key, value in gates.items()}
            for device, gates in self.preferences.items()
        }

    def _preferences(self, device_id):
        groups = {
            source: {label: [] for label in evidence.LABELS} for source in ("human", "automatic")
        }
        for row in self.inputs[device_id]["rows"]:
            label = evidence.room_label(self.inputs, row[0])
            if label:
                state, source, confidence = label
                groups[source][state].append((row[0], row[1], state, confidence))
        return {
            key: gate_preference(key, groups["human"], groups["automatic"], value)
            for key, value in self.current[device_id].items()
        }

    def active(self, source, label, spans):
        return sum(self.mass[source, label].between(a, b) for a, b in spans)

    def rates(self, thresholds):
        projected = [
            radar.project(radar.bits(thresholds[key])) for key, radar in self.radars.items()
        ]
        detected = union(span for item in projected for span in item["lower"])
        false = union(span for item in projected for span in item["upper"])
        rates = {}
        for source in ("human", "automatic"):
            p, n = self.totals[source, "present"], self.totals[source, "not_present"]
            rates[source] = (
                1 - self.active(source, "present", detected) / p if p else 0,
                self.active(source, "not_present", false) / n if n else 0,
            )
        return rates

    def costs(self, thresholds):
        rates = self.rates(thresholds)
        preferred = [
            rates["human" if self.totals["human", label] else "automatic"][index]
            for index, label in enumerate(evidence.LABELS)
        ]
        return error_cost(*preferred), error_cost(*rates["automatic"])

    def rank(self, thresholds):
        rates = self.rates(thresholds)
        cost, automatic = (error_cost(*rates[source]) for source in ("human", "automatic"))
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
        return round(cost, 9), round(automatic, 9), supported_margin, len(changed), margin


def _truth_row(rows, start, label):
    for (source, state), samples in rows.items():
        weight = label[2] if label and label[:2] == (state, source) else 0
        samples.append((start, {}, state, weight))


def supported(preference):
    return (
        preference["separated"]
        and preference["noise_ceiling"]
        < preference["preferred_threshold"]
        < preference["presence_reference"]
    )

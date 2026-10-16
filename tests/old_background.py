"""Reference: the reference folding and room attribution as they were before they were made fast.

Plain and slow, one sample at a time. The efficiency tests check the fast paths against these on
random inputs; nothing else uses them."""

import math

import test_tuner  # noqa: F401  (registers tuner_under_test)
from tuner_under_test.const import HISTORY_KEYS
from tuner_under_test.history import guesses
from tuner_under_test.presence import references as refs


def observe_many(profile, observations):
    """``references.observe_many``: a period lookup, trim scan and key scan for every sample."""
    period, histograms = None, {}
    for timestamp, values, state, source, confidence in observations:
        current = _period(profile, state, source, confidence, timestamp)
        if current is None:
            continue
        if current is not period:
            _store_period(period, histograms)
            period = current
            histograms = {key: dict(bins) for key, bins in refs.decode(period["data"]).items()}
        _add_values(histograms, values)
        period["end"] = timestamp
        period["count"] += 1
        period["confidence"] += (confidence - period["confidence"]) / period["count"]
    _store_period(period, histograms)


def _store_period(period, histograms):
    if period is not None:
        period["data"] = refs.encode(histograms)


def _period(profile, state, source, confidence, timestamp):
    if state not in refs.STATES or source not in refs.SOURCE_WEIGHT or confidence <= 0:
        return None
    periods = profile["periods"]
    previous = next((p for p in reversed(periods) if p["source"] == source), None)
    if previous and timestamp <= previous["end"]:
        return None
    if not refs._continues(previous, state, timestamp):
        previous = {
            "source": source,
            "state": state,
            "start": timestamp,
            "end": timestamp,
            "confidence": 0.0,
            "count": 0,
            "data": refs.encode({}),
        }
        periods.append(previous)
    matching = [p for p in periods if (p["source"], p["state"]) == (source, state)]
    if len(matching) > refs.MAX_PERIODS:
        periods.remove(matching[0])
    return previous


def _add_values(histograms, values):
    for key, value in values.items():
        if key in HISTORY_KEYS and math.isfinite(value) and 0 <= value <= 100:
            bins = histograms.setdefault(key, {})
            index = str(round(value))
            bins[index] = bins.get(index, 0) + 1


# --- The registry walks of every poll: one full walk of the registry for each question.


def discover_devices(registry):
    from tuner_under_test.const import GATE_RE

    devices = {}
    for entity in registry.entities.values():
        if entity.domain != "sensor" or not entity.device_id:
            continue
        match = GATE_RE.match(entity.entity_id.split(".", 1)[1])
        if not match or match.group("metric") != "energy":
            continue
        devices.setdefault(entity.device_id, {"entities": {}})
        devices[entity.device_id]["entities"][entity.entity_id] = {
            "gate": int(match.group("gate")),
            "kind": match.group("kind"),
        }
    return devices


def refresh_devices(known, registry):
    """``discovery.refresh_devices`` on the devices dictionary ``known``."""
    found = discover_devices(registry)
    for existing_id, existing in known.items():
        if existing_id not in found:
            existing["entities"] = {}
    for device_id, info in found.items():
        known.setdefault(device_id, {"recording_enabled": False})
        known[device_id]["entities"] = info["entities"]
        known[device_id].setdefault("training_state", "unknown")


def snapshot_thresholds(runtime, device_id, registry):
    from tuner_under_test.const import GATE_RE
    from tuner_under_test.presentation import snapshots

    current = {}
    for entity in registry.entities.values():
        if entity.device_id != device_id or entity.domain != "number":
            continue
        match = GATE_RE.match(entity.entity_id.split(".", 1)[1])
        if match and match.group("metric") == "threshold":
            key = f"g{match.group('gate')}_{match.group('kind')}"
            value = snapshots._threshold_reading(runtime.hass.states.get(entity.entity_id))
            if value is not None:
                current[key] = value
    return current


def candidates(runtime, device_id, registry):
    from tuner_under_test.calibration import timing_config

    found = {field: [] for field in timing_config.FIELDS}
    for entity in registry.entities.values():
        if entity.device_id != device_id or entity.domain not in ("number", "sensor"):
            continue
        field = timing_config._field(entity)
        if field:
            found[field].append(entity.entity_id)
    return found


def threshold_configuration(runtime, device_id, registry):
    from tuner_under_test.calibration import device_io, service
    from tuner_under_test.const import GATE_RE

    entities, current, limits = {}, {}, {}
    for entity in registry.entities.values():
        if entity.device_id != device_id or entity.domain != "number":
            continue
        value = device_io.number_value(runtime.hass.states.get(entity.entity_id))
        service._distance_limit(entity.entity_id, value, limits)
        match = GATE_RE.match(entity.entity_id.split(".", 1)[1])
        if match and match.group("metric") == "threshold":
            key = f"g{match.group('gate')}_{match.group('kind')}"
            entities[key] = entity.entity_id
            if math.isfinite(value):
                current[key] = device_io.threshold_value(value)
    entities = {
        key: entity_id
        for key, entity_id in entities.items()
        if int(key[1]) <= limits.get(key.split("_")[1], 8)
    }
    expected = {
        f"g{gate}_{kind}" for kind in ("move", "still") for gate in range(limits.get(kind, 8) + 1)
    }
    missing = expected - set(entities)
    if missing:
        raise ValueError(
            "Enable all active gate threshold entities before learning: "
            + ", ".join(sorted(missing))
        )
    current = {key: value for key, value in current.items() if key in entities}
    return entities, current


# --- The room estimate of every poll: the whole last hour of every member read each time.


def recent(runtime, key, since):
    device = runtime.data["devices"].get(key)
    if device is None:
        return []
    return sorted(ts for ts, _row in runtime._iter_history_samples(device, since=since))


def anchor(times, cells):
    from bisect import bisect_right

    anchors = []
    for key, stamps in times.items():
        if cells[key]:
            sample = stamps[bisect_right(stamps, cells[key][-1][1]) - 1]
            if sample == stamps[-1]:
                anchors.append(sample)
    return max(anchors, default=None)


def current_estimate(runtime, device_ids):
    """``room_labels.current_estimate``."""
    import time

    from tuner_under_test.calibration.constants import SAMPLE_SECONDS
    from tuner_under_test.history import guesses
    from tuner_under_test.rooms import room_labels, truth

    now = time.time()
    since = now - room_labels.FEEDBACK_MAX_SECONDS
    times = {key: recent(runtime, key, since) for key in device_ids}
    runs = {key: guesses.evidence(runtime, key, since) for key in device_ids}
    cells = {
        key: [(max(a, since), b) for a, b, _ in item.cells(since)] for key, item in runs.items()
    }
    read = truth.estimate_reader({key: {"automatic": item} for key, item in runs.items()})
    latest = max((rows[-1] for rows in times.values() if rows), default=None)
    found = anchor(times, cells)
    if found is None or latest - found > SAMPLE_SECONDS:
        return None
    if now - found > room_labels.FEEDBACK_WINDOW_SECONDS:
        return None
    edges = sorted(
        {edge for items in cells.values() for span in items for edge in span if edge <= found}
    )
    return room_labels._stretch(read, edges, found)


# --- Attribution of a radar's occupied episodes: a pass over every sample for each question.


def _state(label):
    return label[0] if label else None


def _ends(run, flag, t, gap):
    return run is not None and (not flag or t - run[1] > gap)


def _runs(flags, gap):
    run = None
    for t, flag in flags:
        if _ends(run, flag, t, gap):
            yield run
            run = None
        if flag:
            run = (run[0] if run else t, t)
    if run:
        yield run


GAP = guesses.GAP


def episodes(truth_at, times, gap=GAP):
    return list(_runs(((t, _state(truth_at(t)) == "present") for t in times), gap))


def baseline(rows, quiet_at):
    values = {}
    for t, gates, *_ in rows:
        if quiet_at(t):
            for key, value in gates.items():
                values.setdefault(key, []).append(value)
    return {key: sorted(items) for key, items in values.items()}


def _sustained(rows, key, ceiling):
    from tuner_under_test.calibration.constants import (
        ATTRIBUTION_SUSTAIN_SECONDS,
        SAMPLE_SECONDS,
    )

    above = ((t, gates.get(key, -1) > ceiling) for t, gates, *_ in rows)
    return any(
        last - first + SAMPLE_SECONDS >= ATTRIBUTION_SUSTAIN_SECONDS
        for first, last in _runs(above, GAP)
    )


def _human(rows, radar_label_at):
    states = {radar_label_at(row[0]) for row in rows}
    for state, answer in (("present", "yes"), ("not_present", "no")):
        if state in states:
            return {"state": answer, "confidence": 1.0}
    return None


def attribute(rows, episode, base, radar_label_at):
    from tuner_under_test.calibration.constants import ATTRIBUTION_FULL_CONFIDENCE_SAMPLES
    from tuner_under_test.presence.inference import MIN_REFERENCE
    from tuner_under_test.rooms import attribution

    inside = [row for row in rows if episode[0] <= row[0] <= episode[1]]
    decided = _human(inside, radar_label_at)
    if decided:
        return decided
    usable = {key: values for key, values in base.items() if len(values) >= MIN_REFERENCE}
    if not usable:
        return {"state": "unknown", "confidence": 0.0}
    seen = [
        key
        for key, values in usable.items()
        if _sustained(inside, key, attribution._ceiling(values))
    ]
    support = max(len(usable[key]) for key in seen or usable)
    return {
        "state": "yes" if seen else "no",
        "confidence": min(1.0, support / ATTRIBUTION_FULL_CONFIDENCE_SAMPLES),
    }


def _found(rows, times, truth_at, base, radar_label_at):
    from bisect import bisect_left, bisect_right

    found = []
    for span in episodes(truth_at, times):
        inside = rows[bisect_left(times, span[0]) : bisect_right(times, span[1])]
        result = attribute(inside, span, base, radar_label_at)
        found.append((span, {**result, "inferred": _human(inside, radar_label_at) is None}))
    return found


def quiet_reader(radar_label_at, group_truths):
    def quiet(t):
        return radar_label_at(t) == "not_present" or all(
            _state(truth_at(t)) == "not_present" for truth_at in group_truths.values()
        )

    return quiet


def attributions(radar, rows, radar_label_at, group_truths):
    base = baseline(rows, quiet_reader(radar_label_at, group_truths))
    times = [row[0] for row in rows]
    return {
        group: {radar: _found(rows, times, truth_at, base, radar_label_at)}
        for group, truth_at in group_truths.items()
    }


# --- The evidence a joint fit reads: a dictionary of readings for every sample.


def evidence_rows(view, device_id, keys, start, end):
    """``evidence.read``'s ``(rows, barriers)`` from the recorded spans up to ``end``: a span
    where any of ``keys`` read is a row ``(span start, {gate: energy rounded up}, "unknown")``;
    one where none of them read is a barrier at its start."""
    from tuner_under_test.history import spans

    rows, barriers = [], []
    for cell in spans.events(view, device_id, end=end):
        values = {
            key: math.ceil(cell.values[HISTORY_KEYS.index(key)])
            for key in keys
            if cell.values[HISTORY_KEYS.index(key)] is not None
        }
        if values:
            rows.append((cell.start, values, "unknown"))
        else:
            barriers.append(cell.start)
    return rows, sorted(set(barriers))


# --- A reference rebuild reads the whole history twice more: once for human labels, once for room evidence.


def human_observations(runtime, device, label_at, room=None):
    from tuner_under_test.presence import reference_training as training

    for timestamp, row in runtime._iter_history_samples(device):
        label = label_at(timestamp)
        if label is None and room is not None:
            label = training._room_human(room(timestamp))
        if label in refs.STATES:
            values = {
                key: value for key, value in zip(HISTORY_KEYS, row, strict=False) if value <= 100
            }
            yield timestamp, values, label, "human", 1.0


def room_observations(runtime, device, auto, label_at):
    from tuner_under_test.presence import reference_training as training

    for timestamp, row in runtime._iter_history_samples(device):
        guess = None if label_at(timestamp) else auto(timestamp)
        if guess and guess[1] in training.SOURCE_OF:
            values = {
                key: value for key, value in zip(HISTORY_KEYS, row, strict=False) if value <= 100
            }
            yield timestamp, values, guess[0], training.SOURCE_OF[guess[1]], guess[2]

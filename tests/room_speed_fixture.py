"""A synthetic room with the shape of a real long-running store; nothing recorded is used.

Two radars, retained days of six-second cells whose readings change in most cells, about 248
automatic sensor labels a day with edges off the six-second grid, a few long human labels, the
radars' own recorded guesses and one open partial day. ``request`` captures it as the worker
receives it, so the production ``room_fit`` path reads production files.
"""

import random
from array import array
from pathlib import Path
from types import SimpleNamespace

DAY = 86400
START = 1_790_000_000 // DAY * DAY
CELLS = DAY // 6
GATES = 18
RADARS = ("radar_a", "radar_b")
GROUP = "room"
LABELS_PER_DAY = 248
QUIET = bytes(4 + value % 14 for value in range(256))
BUSY = bytes(38 + value % 40 for value in range(256))


def _sensor_labels(index, rng):
    """About ``LABELS_PER_DAY`` alternating stretches with millisecond edges off the grid."""
    start = START + index * DAY
    step = DAY / LABELS_PER_DAY
    labels = []
    for at in range(LABELS_PER_DAY):
        low = start + at * step + rng.randrange(1, 40000) / 1000
        high = low + step * 0.8 + rng.randrange(1, 9000) / 1000
        labels.append(
            {
                "start": low,
                "end": min(high, start + DAY),
                "state": "present" if at % 3 == 0 else "not_present",
                "source": "sensor",
                "confidence": 0.8,
            }
        )
    return labels


def _human_labels(days):
    """A few long stretches near the end of the history, days apart."""
    labels = []
    for back, state in ((2, "present"), (3, "not_present"), (5, "present"), (6, "not_present")):
        low = START + (days - back) * DAY + 21600.125
        labels.append({"start": low, "end": low + 14400.5, "state": state, "source": "manual"})
    return labels


def _pick(busy, high, low):
    return high if busy else low


def _column(rng, count, gate, busy, radar):
    """One gate's readings: occupied cells read high on the radar's nearer gates."""
    noise = rng.randbytes(count)
    if gate % (radar + 2) == 0:
        return bytes(map(_pick, busy, noise.translate(BUSY), noise.translate(QUIET)))
    return noise.translate(QUIET)


def _occupied(labels, day_start, count):
    busy = bytearray(count)
    for label in labels:
        if label["state"] == "present":
            first = int((label["start"] - day_start) // 6)
            stop = min(count, int((label["end"] - day_start) // 6) + 1)
            if first < stop:
                busy[first:stop] = b"\x01" * (stop - first)
    return bytes(busy)


def _guesses(labels, stop):
    runs = []
    for label in labels[::6]:
        if label["start"] < min(label["end"], stop):
            runs.append((label["start"], min(label["end"], stop), label["state"], 80, 0))
    return runs


def _grid(start, cadence, rng):
    """Contiguous rows ``cadence`` seconds apart; below six seconds their times are irregular
    milliseconds, as a radar's changes arrive."""
    count = DAY // cadence
    if cadence == 6:
        times = array("d", range(start, start + DAY, 6))
    else:
        offsets = rng.randbytes(count)
        times = array(
            "d",
            (start + at * cadence + (offsets[at] % 200) / 1000 * (at > 0) for at in range(count)),
        )
    return times, array("d", (*times[1:], start + DAY))


def _finished(radar, index, labels, rng, cadence):
    from tuner_under_test.history import dayfile

    start = START + index * DAY
    times, ends = _grid(start, cadence, rng)
    busy = _occupied(labels, start, CELLS)
    busy = bytes(busy[int(at - start) // 6] for at in times) if cadence != 6 else busy
    columns = [_column(rng, len(times), gate, busy, radar) for gate in range(GATES)]
    return dayfile.Day(start, start + DAY, times, ends, columns, _guesses(labels, start + DAY))


def _open_day(store, rid, radar, index, labels, rng, cells):
    from tuner_under_test.history import dayfile

    start = START + index * DAY
    busy = _occupied(labels, start, cells)
    columns = [_column(rng, cells, gate, busy, radar) for gate in range(GATES)]
    for cell in range(cells):
        values = {gate: columns[gate][cell] for gate in range(GATES)}
        store.append(rid, dayfile.Change(start + cell * 6, values))
    store.append(rid, dayfile.End(start + cells * 6))


def build(root, days=60, *, open_cells=1800, sensor_days=10, cadence=6):
    """Write ``days`` retained days per radar (the last one open, ``open_cells`` long).

    Saved metadata has a fixed node budget, so only the newest ``sensor_days`` keep their
    sensor labels, as ``room_labels.expire_sensor`` keeps a real store within it; recorded
    guesses cover every day.
    """
    from tuner_under_test.history import daystore

    store = daystore.DayStore(Path(root) / "history", cache_bytes=0)
    label_rng = random.Random(2410)
    per_day = [_sensor_labels(index, label_rng) for index in range(days)]
    until = START + (days - 1) * DAY + open_cells * 6
    sensor = [
        {**label, "end": min(label["end"], until)}
        for labels in per_day[-sensor_days:]
        for label in labels
        if label["start"] < until
    ]
    for radar, rid in enumerate(RADARS):
        rng = random.Random(7000 + radar)
        for index in range(days - 1):
            day = _finished(radar, index, per_day[index], rng, cadence)
            store.replace(rid, daystore.date_of(day.start), day)
        _open_day(store, rid, radar, days - 1, per_day[-1], rng, open_cells)
    return SimpleNamespace(
        store=store, days=days, until=until, labels=[*sensor, *_human_labels(days)]
    )


def _snapshot(fixture, rid):
    return {
        "device_id": rid,
        "own": [GROUP],
        "groups": {GROUP: {"device_ids": list(RADARS)}},
        "room_labels": {GROUP: fixture.labels},
        "members": {key: {"history_labels": [], "training_state": "unknown"} for key in RADARS},
    }


def request(fixture, directory, *, window_days=28, attribution_days=7, weight=0.5):
    """The ``room_fit`` worker request the integration would capture for this store."""
    from tuner_under_test.const import HISTORY_KEYS
    from tuner_under_test.worker import capture, codec, manifest

    until = fixture.until
    devices = {key: {"history_labels": [], "training_state": "unknown"} for key in RADARS}
    views = {
        rid: SimpleNamespace(
            data={"devices": devices, "room_snapshot": _snapshot(fixture, rid)},
            _days=fixture.store,
            _history_until=until,
            _snapshot_history_start=START,
            _physical_history_start=START,
            _attribution_since=(until - attribution_days * DAY) // 3600 * 3600,
            _attribution_until=until,
        )
        for rid in RADARS
    }
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with manifest.Writer(directory / "history.sqlite") as writer:
        capture.prepare_multi(views, directory, sink=writer)
    members = {
        rid: {"view_key": rid, "thresholds": dict.fromkeys(HISTORY_KEYS, 50), "timing": {}}
        for rid in RADARS
    }
    room = {"group_id": GROUP, "groups": {}, "room_labels": {GROUP: fixture.labels}}
    return {
        "manifest": str(writer.path),
        "operation": "room_fit",
        "parameters": codec.encode(
            {
                "members": {GROUP: members},
                "rooms": {GROUP: room},
                "start": until - window_days * DAY,
                "end": until,
                "weight": weight,
            }
        ),
    }

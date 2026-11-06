"""A synthetic room with the shape of a real long-running store; nothing recorded is used.

The shape was measured on a real store through the production reader (anonymous statistics
only): about 48,000 rows a radar-day, almost none on a six-second clock, 99.9% contiguous, half
under half a second, a quarter about a second, the rest two to many seconds; 30% of rows repeat
the last on a ten-channel radar and 80% on a two-channel one; 0.2% miss a channel. Added to that:
about 248 automatic sensor labels a day with edges off any clock, a few long human labels, the
radars' own recorded guesses and one open partial day. ``request`` captures it as the worker
receives it, so the production ``room_fit`` path reads production files.
"""

import random
from array import array
from itertools import accumulate
from pathlib import Path
from types import SimpleNamespace

DAY = 86400
START = 1_790_000_000 // DAY * DAY
GATES = 18
RADARS = {"radar_a": (10, 0.30), "radar_b": (2, 0.80)}  # Channels read, share of repeated rows.
GROUP = "room"
LABELS_PER_DAY = 248
QUIET = bytes(4 + value % 14 for value in range(256))
BUSY = bytes(38 + value % 40 for value in range(256))
MISSING = 255
# (milliseconds low, high, weight) of a row's duration.
DURATIONS = (
    (40, 499, 50),
    (900, 1100, 25),
    (1900, 2100, 6),
    (5900, 6100, 10),
    (6900, 7100, 3),
    (8000, 20000, 6),
)
GAP_SHARE = 0.001
MISSING_SHARE = 0.002


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


def _column(rng, sources, gate, busy, radar):
    """One channel's readings: occupied rows read high on the radar's nearer channels; a row
    that repeats the last (``sources``: the row each takes its vector from) reads as it did."""
    noise = rng.randbytes(len(sources))
    if gate % (radar + 2) == 0:
        fresh = bytes(map(_pick, busy, noise.translate(BUSY), noise.translate(QUIET)))
    else:
        fresh = noise.translate(QUIET)
    return bytes(map(fresh.__getitem__, sources))


def _columns(rng, rows, channels, repeated, busy, radar):
    flags = rng.choices((0, 1), (1 - repeated, repeated), k=rows)
    sources = list(accumulate(range(rows), lambda last, at: last if flags[at] else at))
    read = [_column(rng, sources, gate, busy, radar) for gate in range(channels)]
    missing = bytes(MISSING for _ in range(rows))
    columns = [*read, *(missing for _ in range(GATES - channels))]
    return _with_missing(rng, columns, channels, rows)


def _with_missing(rng, columns, channels, rows):
    holes = rng.sample(range(rows), int(rows * MISSING_SHARE))
    edited = [bytearray(column) for column in columns[:channels]]
    for row in holes:
        edited[row % channels][row] = MISSING
    return [*map(bytes, edited), *columns[channels:]]


def _rows(start, seconds, rng):
    """Row times and ends in whole milliseconds, off any clock, nearly always contiguous."""
    weights = [item[2] for item in DURATIONS]
    times, ends = array("d"), array("d")
    at, stop = 0, seconds * 1000
    while at < stop:
        low, high, _ = rng.choices(DURATIONS, weights)[0]
        finish = min(stop, at + rng.randrange(low, high))
        times.append(start + at / 1000)
        ends.append(start + finish / 1000)
        at = finish + (rng.randrange(1000, 30000) if rng.random() < GAP_SHARE else 0)
    return times, ends


def _occupied(labels, times):
    """Per row whether a Present label holds at its start."""
    busy = bytearray(len(times))
    present = sorted(
        (label["start"], label["end"]) for label in labels if label["state"] == "present"
    )
    at = 0
    for row, time in enumerate(times):
        while at < len(present) and present[at][1] <= time:
            at += 1
        busy[row] = at < len(present) and present[at][0] <= time
    return bytes(busy)


def _guesses(labels, stop):
    runs = []
    for label in labels[::6]:
        if label["start"] < min(label["end"], stop):
            runs.append((label["start"], min(label["end"], stop), label["state"], 80, 0))
    return runs


def _finished(radar, index, labels, rng, shape):
    from tuner_under_test.history import dayfile

    start = START + index * DAY
    times, ends = _rows(start, DAY, rng)
    busy = _occupied(labels, times)
    columns = _columns(rng, len(times), *shape, busy, radar)
    return dayfile.Day(start, start + DAY, times, ends, columns, _guesses(labels, start + DAY))


def _open_day(store, rid, radar, index, labels, rng, shape, seconds):
    from tuner_under_test.history import dayfile

    start = START + index * DAY
    times, ends = _rows(start, seconds, rng)
    columns = _columns(rng, len(times), *shape, _occupied(labels, times), radar)
    for row, (time, end) in enumerate(zip(times, ends, strict=True)):
        values = {
            gate: None if column[row] == MISSING else column[row]
            for gate, column in enumerate(columns)
        }
        store.append(rid, dayfile.Change(time, values))
        if row + 1 == len(times) or times[row + 1] != end:
            store.append(rid, dayfile.End(end))
    return ends[-1]


def build(root, days=60, *, open_seconds=10800, sensor_days=10):
    """Write ``days`` retained days per radar (the last one open, ``open_seconds`` long).

    Saved metadata has a fixed node budget, so only the newest ``sensor_days`` keep their
    sensor labels, as ``room_labels.expire_sensor`` keeps a real store within it; recorded
    guesses cover every day.
    """
    from tuner_under_test.history import daystore

    store = daystore.DayStore(Path(root) / "history", cache_bytes=0)
    label_rng = random.Random(2410)
    per_day = [_sensor_labels(index, label_rng) for index in range(days)]
    until = START + (days - 1) * DAY + open_seconds
    sensor = [
        {**label, "end": min(label["end"], until)}
        for labels in per_day[-sensor_days:]
        for label in labels
        if label["start"] < until
    ]
    for radar, (rid, shape) in enumerate(RADARS.items()):
        rng = random.Random(7000 + radar)
        for index in range(days - 1):
            day = _finished(radar, index, per_day[index], rng, shape)
            store.replace(rid, daystore.date_of(day.start), day)
        _open_day(store, rid, radar, days - 1, per_day[-1], rng, shape, open_seconds)
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
        rid: {
            "view_key": rid,
            "thresholds": dict.fromkeys(HISTORY_KEYS[: RADARS[rid][0]], 50),
            "timing": {},
        }
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

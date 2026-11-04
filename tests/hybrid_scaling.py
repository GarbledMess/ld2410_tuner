"""Deterministic raw day files for scaling checks; no recorded history is used."""

from array import array

from tuner_under_test.history import dayfile, daystore

DAY = 86400
START = 1_790_000_000 // DAY * DAY
CHANNELS = tuple(range(18))


def _labels(start):
    return [
        {
            "start": start + hour * 3600,
            "end": start + (hour + 2) * 3600,
            "state": "present" if hour in (6, 10) else "not_present",
        }
        for hour in (6, 8, 10, 12)
    ]


def _times(density):
    step = 6 / density
    for row in range(DAY * density // 6):
        offset = row * step
        if 10800 <= offset < 10860:
            continue
        yield offset
        if offset % 900 == 0:
            yield offset + 0.125
            yield offset + 0.25


def _energy(radar, channel, offset, day):
    # Pairs share the same waveform; alternate pairs have opposite responses.
    occupied = int(offset) // 3600 in (6, 7, 10, 11, 18, 19)
    active = occupied if radar % 4 < 2 else not occupied
    noise = (int(offset * 8) * 17 + channel * 11 + radar // 2 + day * 7) % 9
    value = 8 + noise + (35 + channel % 5 if active else 0)
    return 95 if int(offset) % 7200 < 6 else value


def _day(radar, day, density):
    start = START + day * DAY
    offsets = tuple(_times(density))
    times = array("d", (start + offset for offset in offsets))
    ends = array("d", (*times[1:], start + DAY))
    before_gap = times.index(start + 10860) - 1
    ends[before_gap] = start + 10800
    columns = [
        bytes(_energy(radar, channel, offset, day) for offset in offsets) for channel in CHANNELS
    ]
    return dayfile.Day(start, start + DAY, times, ends, columns, [])


def _live(store, rid, day):
    for index, (start, end) in enumerate(zip(day.times, day.ends, strict=True)):
        store.append(rid, dayfile.Change(start, dict(enumerate(day.vector(index)))))
        if end < day.end and end < day.times[index + 1]:
            store.append(rid, dayfile.End(end))


def build_fixture(root, radars=30, days=28, *, density=1):
    """Write one day at a time, keeping only each radar's final day live.

    Each day covers midnight to midnight except one 60-second outage. Labels
    cover 06:00-14:00. Base rows are six seconds apart (three at density two),
    with two 125-ms burst rows every 15 minutes. No cell digests are prepared.
    """
    for name, value in (("radars", radars), ("days", days), ("density", density)):
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    store = daystore.DayStore(root / "history", cache_bytes=0)
    devices, rows = {}, 0
    labels = [label for day in range(days) for label in _labels(START + day * DAY)]
    for radar in range(radars):
        rid = f"r{radar}"
        devices[rid] = {"history_labels": labels, "training_state": "unknown"}
        for index in range(days):
            day = _day(radar, index, density)
            rows += len(day.times)
            if index == days - 1:
                _live(store, rid, day)
            else:
                store.replace(rid, daystore.date_of(day.start), day)
    return {
        "store": store,
        "devices": devices,
        "radar_days": radars * days,
        "source_rows": rows,
        "base_cadence_seconds": 6 / density,
        "labelled_duty_cycle": 1 / 3,
    }

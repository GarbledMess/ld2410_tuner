"""A synthetic recorded radar shaped like a real one, for Learn benchmarks and parity tests.

No private input: every reading comes from a small fixed generator. Readings change in
most cells, the classes overlap, sensor labels have edges off the six-second clock, a few
long human labels exist, the recorder has outages, and the newest day is still open.
"""

import datetime
from types import SimpleNamespace

from tuner_under_test.history import dayfile
from tuner_under_test.worker import history
from tuner_under_test.worker.calculations import ReadOnlyDays

DAY = 86400
GATES = 18
SENSOR_LABELS = 248
LABEL_SECONDS = 348
OUTAGES = 12


def _noise(day, row, gate):
    return ((day * 7919 + row * 104729 + gate * 1299709) * 2654435761 >> 13) & 0xFFFF


def _occupied(day, row):
    """Occupancy that mostly agrees with the sensor labels, with some disagreement."""
    label = row * 6 // LABEL_SECONDS
    return label % 2 == 0 and (_noise(day, label, 31) % 11 != 0)


def _reading(day, row, gate, occupied):
    noise = _noise(day, row, gate)
    level = 42 if occupied else 6
    reading = level + noise % 23 - gate
    if not occupied and noise % 97 == 0:
        reading += 45  # A false burst from a fan or a curtain.
    return max(0, min(100, reading))


def _row_values(day, row, irregular):
    occupied = _occupied(day, row)
    values = {g: _reading(day, row, g, occupied) for g in range(GATES)}
    if irregular and _noise(day, row, 40) % 503 == 0:
        values[_noise(day, row, 41) % GATES] = None  # One gate stopped reporting.
    return values


def _outages(day, rows):
    """Row ranges the recorder missed: several a day, of different lengths."""
    found = {}
    for k in range(OUTAGES):
        first = _noise(day, k, 50) % max(1, rows - 60)
        for row in range(first, first + 3 + _noise(day, k, 51) % 40):
            found[row] = True
    return found


def _add_rows(builder, day, start, rows, irregular):
    outages = _outages(day, rows)
    previous = False
    for row in range(rows):
        missed = row in outages
        if missed and not previous:
            builder.add(dayfile.End(start + row * 6))
        if not missed:
            # The recorder restarts off the clock, then settles back onto it.
            shift = 1.5 if irregular and previous else 0.0
            builder.add(dayfile.Change(start + row * 6 + shift, _row_values(day, row, irregular)))
            if irregular and _noise(day, row, 42) % 1009 == 0:
                burst = _row_values(day, row + 1, irregular)
                builder.add(dayfile.Change(start + row * 6 + 2.25, burst))
        previous = missed
    builder.add(dayfile.End(start + rows * 6))


def _guesses(start, rows):
    return [
        (
            start + i * LABEL_SECONDS + 0.125,
            min(start + rows * 6, start + (i + 1) * LABEL_SECONDS + 0.125),
            "present" if i % 2 == 0 else "not_present",
            18,
            i % 3,
        )
        for i in range(SENSOR_LABELS)
        if i * LABEL_SECONDS < rows * 6
    ]


def _write_day(path, day, rows, irregular):
    if path.exists():
        return
    start = day * DAY
    builder = dayfile.DayBuilder(start, start + DAY)
    _add_rows(builder, day, start, rows, irregular)
    physical = builder.day()
    physical.guesses = _guesses(start, rows)
    path.write_bytes(dayfile.encode_day(physical))


def _human_labels(day):
    start = day * DAY
    return [
        {"start": start + 1000.25, "end": start + 3000.875, "state": "present"},
        {"start": start + 4000.5, "end": start + 6000.125, "state": "not_present"},
    ]


def _entry(path, day, count):
    date = datetime.date(1970, 1, 1) + datetime.timedelta(days=day)
    return {
        "device_id": "r",
        "date": str(date),
        "path": str(path),
        "identity": history._identity(path.stat()),
        "late": [],
        "persistent": day < count,
    }


def recorded(root, days=60, *, irregular=False, open_rows=2400, human_every=10):
    """A worker view of ``days`` finished days plus one open partial day, kept under ``root``."""
    folder = root / "r"
    folder.mkdir(parents=True, exist_ok=True)
    entries, labels = [], []
    for day in range(days + 1):
        path = folder / f"{day}.day"
        _write_day(path, day, 14400 if day < days else open_rows, irregular)
        entries.append(_entry(path, day, days))
        if day % human_every == 0:
            labels.extend(_human_labels(day))
    budgets = {"max_file_bytes": None, "max_raw_bytes": 1 << 30, "max_rows": None}
    until = days * DAY + open_rows * 6
    return SimpleNamespace(
        data={
            "devices": {"r": {"history_labels": labels, "training_state": "unknown"}},
            "room_snapshot": None,
        },
        hass=None,
        _days=ReadOnlyDays({"days": entries, "budgets": {**budgets, "max_guesses": None}}),
        _history_until=until,
        _attribution_until=until,
    )

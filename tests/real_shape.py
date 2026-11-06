"""One realistic synthetic home, the shape of a real install but entirely made up.

Twelve radars in two-radar and one-radar rooms; sixty retained days of day files of about
0.4 MB each, whose readings change in most six-second cells except for quiet overnight sleepers
and a pet that wanders by; about 248 automatic sensor labels a day with edges off the six-second
grid; a handful of long human labels and a few short ones; manual room labels covering several
radars; one open partial day; tens of thousands of metadata items in the saved JSON.

``build(folder)`` builds it once (seeded, so byte-identical every time). ``open_copy`` gives a
test its own copy cheaply: finished day files are immutable and always replaced by rename, so
they are hard links, and only what is appended to or rewritten in place is copied.
"""

import datetime
import json
import os
import random
import shutil
import zlib
from array import array
from concurrent.futures import ThreadPoolExecutor
from itertools import chain, compress, repeat
from operator import add, mul, sub
from pathlib import Path

import ha_boundary

ha_boundary.load_runtime()

from tuner_under_test.const import DOMAIN, HISTORY_KEYS  # noqa: E402, F401
from tuner_under_test.history import dayfile, daystore  # noqa: E402
from tuner_under_test.presence import references  # noqa: E402

DAY = 86400
# A fixed "now": 01:00 UTC, an hour into the open partial day.
NOW = datetime.datetime(2026, 3, 15, 1, tzinfo=datetime.UTC).timestamp()
TODAY = NOW - NOW % DAY
RETAINED_DAYS = 60
SENSOR_LABELS_PER_DAY = 248
SENSOR_LABEL_DAYS = 15  # Bermuda was added two weeks ago; older days have no sensor labels.
SENSOR_ROOMS = ("living_room", "kitchen", "main_bedroom", "office")
ROW_SECONDS = 3  # Engineering-mode readings arrive about this often when anything changes.
ROWS = DAY // ROW_SECONDS
PARTIAL_SECONDS = 6
DISTINCT_DAYS = {"living_room", "office", "kitchen", "hall"}
# Radars added lately: everything else has the whole retained history.
INSTALLED_DAYS = {"kitchen": 2, "hall": 2, "garage": 2}
STORE_FOLDER = "storage"  # The test Home Assistant keeps its store in this folder of its config.

ROOMS = {
    "living_room": ("Living Room", 2),
    "kitchen": ("Kitchen", 2),
    "main_bedroom": ("Main Bedroom", 2),
    "office": ("Office", 1),
    "nursery": ("Nursery", 1),
    "hall": ("Hall", 1),
    "guest_room": ("Guest Room", 1),
    "bathroom": ("Bathroom", 1),
    "garage": ("Garage", 1),
}
AREAS_WITHOUT_RADARS = {"patio": "Patio"}
SLEEPERS = {"main_bedroom", "nursery"}
PETS = {"living_room", "hall"}
RECORDING_OFF = {"garage"}
# The kitchen's second radar advertises a coarse threshold range, as some firmwares do.
LONG_RADAR = "office_1"
COARSE = "kitchen_2"
COARSE_DOMAIN = {"min": 10, "max": 90, "step": 5}
PHONES = {"sensor.ana_phone_area": "Ana's phone", "sensor.ben_phone_area": "Ben's phone"}

SETTINGS = {
    "thin_after_days": 59,
    "minimum_confidence": 50,
    "automatic_days": 60,
    "human_days": 60,
    "max_mib": 1024,
}


def set_now(now):
    """Build the home around another moment (a report on real child processes, which read the
    real clock). Tests use the fixed one."""
    global NOW, TODAY
    NOW, TODAY = now, now - now % DAY


def radars():
    """``{radar id: room}`` for the twelve radars."""
    return {
        f"{room}_{index + 1}": room for room, (_, count) in ROOMS.items() for index in range(count)
    }


def _table(low, spread):
    return bytes(low + value % spread for value in range(256))


QUIET = _table(3, 11)
SLEEPER_STILL = _table(18, 9)
SLEEPER_MOVE = _table(1, 3)
MOVE_PRESENT = _table(50, 16)
STILL_PRESENT = _table(35, 14)
PET_MOVE = _table(25, 12)


def _stretches(rng, count, shortest, longest, first=6 * 3600, last=23 * 3600):
    """``count`` non-overlapping busy stretches of the waking day, as seconds into it."""
    found, at = [], first
    span = (last - first) // max(count, 1)
    for _ in range(count):
        start = at + rng.randrange(span // 4)
        found.append((start, min(start + rng.randint(shortest, longest), at + span)))
        at += span
    return found


def occupied(room, day):
    """When ``room`` is occupied on ``day``, in seconds into the day; both radars see it."""
    rng = random.Random(f"occupied {room} {_pattern_number(room, day)}")
    return _stretches(rng, rng.randint(3, 6), 600, 5400)


def pet_visits(room, day):
    rng = random.Random(f"pet {room} {_pattern_number(room, day)}")
    return _stretches(rng, 5, 30, 120) if room in PETS else []


def _radar_gates(radar):
    """The gates a person in the room reaches: each radar of a room sees them differently."""
    position = int(radar.rsplit("_", 1)[1]) - 1
    return {position + 1, position + 2}


def _fill(column, stretches, table):
    for start, end in stretches:
        low, high = start // ROW_SECONDS, end // ROW_SECONDS
        column[low:high] = column[low:high].translate(table)


def _column(rng, radar, room, name, busy, pets):
    gate, kind = int(name[1]), name[3:]
    column = bytearray(rng.randbytes(ROWS).translate(QUIET))
    if gate in _radar_gates(radar):
        _fill(column, busy, MOVE_PRESENT if kind == "move" else STILL_PRESENT)
        if room in SLEEPERS:
            _fill(column, [(0, 6 * 3600)], SLEEPER_STILL if kind == "still" else SLEEPER_MOVE)
    if gate == 5 and kind == "move":
        _fill(column, pets, PET_MOVE)
    return column


def _row_filter(room):
    """Overnight a sleeper's radar reports only every few cells: nothing changes between."""
    if room not in SLEEPERS:
        return None
    night = 6 * 3600 // ROW_SECONDS
    return [index % 16 == 0 for index in range(night)] + [True] * (ROWS - night)


def _guess_runs(start, busy):
    runs, previous = [], 0
    for low, high in busy:
        if low - previous > 1200:
            runs.append((start + previous + 300.5, start + low - 300.25, "not_present", 8, 1))
        runs.append((start + low + 12.5, start + high - 7.25, "present", 9, 1))
        previous = high
    return runs


def _planes(values):
    raw = values.tobytes()
    return b"".join(raw[plane::4] for plane in range(4))


def _body(radar, room, number):
    """A finished day file's contents after its header: nothing in them depends on the date."""
    rng = random.Random(f"{radar} {number}")
    busy, pets = occupied(room, number), pet_visits(room, number)
    columns = [_column(rng, radar, room, name, busy, pets) for name in HISTORY_KEYS]
    # Readings arrive off the grid, by up to three quarters of a second.
    starts = map(
        add, range(0, DAY * 1000, ROW_SECONDS * 1000), map(mul, rng.randbytes(ROWS), repeat(3))
    )
    selected = _row_filter(room)
    if selected is not None:
        starts, columns = compress(starts, selected), [compress(c, selected) for c in columns]
    starts = array("i", starts)
    steps = array("i", map(sub, starts, chain((0,), starts)))
    # A row lasts until the next one; the last row until midnight.
    ends = array("i", repeat(0, len(starts) - 1))
    ends.append(DAY * 1000 - starts[-1])
    runs = b"".join(
        dayfile.RUN.pack(
            round(low * 1000), round(high * 1000), dayfile.STATES.index(state), level, basis
        )
        for low, high, state, level, basis in _guess_runs(0, busy)
    )
    readings = b"".join(bytes(column) for column in columns)
    contents = _planes(steps) + _planes(ends) + readings + runs
    return len(starts), len(runs) // dayfile.RUN.size, contents


def _pattern_number(room, number):
    """Which day's pattern a room shows: those a Learn is run on never repeat a day; the others
    cycle through a week of them, which keeps building the home quick."""
    return number if room in DISTINCT_DAYS else number - number % 7


def _day_file(body, start):
    rows, runs, contents = body
    header = dayfile.HEADER.pack(
        dayfile.VERSION, start * 1000, (start + DAY) * 1000, rows, runs, *[0] * dayfile.GATES
    )
    # The reader takes any zlib level; the fastest keeps the files about the size of real ones.
    return dayfile.MAGIC + zlib.compress(header + contents, 1)


def recorded_days(room):
    """How many finished days a room's radars have recorded."""
    return INSTALLED_DAYS.get(room, RETAINED_DAYS)


def _publish(path, raw):
    path.write_bytes(raw)
    with path.open("rb") as stream:
        stream.read(1)


def _write_days(history, radar, room):
    bodies = {}
    for ago in range(1, recorded_days(room) + 1):
        number = _pattern_number(room, int(TODAY // DAY) - ago)
        bodies.setdefault(number, _body(radar, room, number))
    return [
        (
            history / daystore._folder_name(radar) / f"{daystore.date_of(start).isoformat()}.day",
            bodies[_pattern_number(room, start // DAY)],
            start,
        )
        for start in (int(TODAY) - ago * DAY for ago in range(1, recorded_days(room) + 1))
    ]


def _partial_day(history, radar, room):
    """Today so far: an append-only log, coverage left open as a crash or restart leaves it."""
    rng = random.Random(f"{radar} {TODAY} partial")
    cells = int(NOW - TODAY) // PARTIAL_SECONDS
    columns = [
        rng.randbytes(cells).translate(
            SLEEPER_STILL if room in SLEEPERS and name.endswith("still") else QUIET
        )
        for name in HISTORY_KEYS
    ]
    jitter = rng.randbytes(cells)
    records = [
        dayfile.Change(
            TODAY + cell * PARTIAL_SECONDS + jitter[cell] * 0.003,
            {gate: column[cell] for gate, column in enumerate(columns)},
        )
        for cell in range(cells)
    ]
    path = history / daystore._folder_name(radar) / f"{daystore.date_of(TODAY).isoformat()}.log"
    path.write_bytes(dayfile.encode_records(records, TODAY))


def _entities(radar):
    return {
        f"sensor.{radar}_g{name[1]}_{name[3:]}_energy": {"gate": int(name[1]), "kind": name[3:]}
        for name in HISTORY_KEYS
    }


def human_labels(room):
    """A handful of long labels and a few short ones, where the room really was what they say."""
    labels = []
    last = recorded_days(room)
    for ago in (1, 3, 9, 17, 26, 41, 55) if last > 3 else (1, 2, 3):
        if ago > last:
            continue
        start = TODAY - ago * DAY
        low, high = occupied(room, int(start // DAY))[0]
        labels.append({"start": start + low + 1.25, "end": start + high - 2.5, "state": "present"})
        labels.append({"start": start + 1800.75, "end": start + 18000.5, "state": "not_present"})
    for ago in (2, 12, 30):
        if ago > last:
            continue
        start = TODAY - ago * DAY
        low, _ = occupied(room, int(start // DAY))[-1]
        labels.append({"start": start + low + 20.5, "end": start + low + 77.25, "state": "present"})
    return sorted(labels, key=lambda label: label["start"])


def _histograms():
    # What a radar's kept learning counts cost in the saved JSON.
    return {
        name: {"present": [3] * 12 + [0] * 8, "not_present": [40] * 12 + [0] * 8}
        for name in HISTORY_KEYS
    }


def _period(rng, source, state, start, digits=3, energies=8):
    """One 30-minute reference period: what the radar read while it held that state, in
    fractions of six-second units because edges and readings are off the grid."""
    centre = 50 if state == "present" else 7
    data = references.encode(
        {
            name: {
                str(centre + offset): round(rng.uniform(1, 40), digits)
                for offset in range(rng.randint(energies // 2, energies))
            }
            for name in HISTORY_KEYS
        }
    )
    return {
        "source": source,
        "state": state,
        "start": start,
        "end": start + 1800.25,
        "confidence": 1.0 if source == "human" else 0.9,
        "count": 300.0,
        "data": data,
    }


def _reference_profile(radar, room):
    """What earlier rebuilds left: a few human periods, more from the rooms' sensors and the
    radar's own guesses. Its size is what the worker's metadata limits have to admit."""
    rng = random.Random(f"reference {radar}")
    counts = {"human": 8, "radar": 12, "entity": 24 if f"area:{room}" in _labelled_rooms() else 0}
    periods = [
        _period(rng, source, state, TODAY - (index + 1) * 2 * DAY)
        for source, count in counts.items()
        for state in ("present", "not_present")
        for index in range(count)
    ]
    periods.sort(key=lambda period: period["start"])
    return {**references.empty(), "periods": periods}


def reference_profile_at_cap(radar):
    """All that a radar is allowed to keep: 24 periods of every source and state, of readings
    with durations off the six-second grid, as a home with plenty of labels ends up with."""
    rng = random.Random(f"reference at cap {radar}")
    periods = [
        _period(rng, source, state, TODAY - (index + 1) * DAY, digits=15, energies=12)
        for source in references.SOURCE_WEIGHT
        for state in references.STATES
        for index in range(references.MAX_PERIODS)
    ]
    periods.sort(key=lambda period: period["start"])
    return {**references.empty(), "periods": periods}


def _labelled_rooms():
    return tuple(f"area:{room}" for room in SENSOR_ROOMS)


def _sensor_labels(room, number, share):
    """One room's share of the home's automatic labels for a day, off the six-second grid."""
    rng = random.Random(f"sensor {room} {number}")
    start, busy, step = number * DAY, occupied(room, number), DAY / share
    labels = []
    for index in range(share):
        low = start + index * step + rng.uniform(0.1, 5.7)
        high = low + step * rng.uniform(0.4, 0.9)
        state = "present" if any(a <= low - start < b for a, b in busy) else "not_present"
        labels.append(
            {"start": low, "end": high, "state": state, "source": "sensor", "confidence": 0.9}
        )
    return labels


def _manual_room_labels(room):
    labels = []
    for ago in (1, 5, 21, 44):
        start = TODAY - ago * DAY
        low, high = occupied(room, int(start // DAY))[1]
        labels.append(
            {
                "start": start + low + 3.5,
                "end": start + high - 4.75,
                "state": "present",
                "source": "manual",
            }
        )
    return labels


def room_labels():
    share = SENSOR_LABELS_PER_DAY // len(SENSOR_ROOMS)
    labels = {}
    for room in SENSOR_ROOMS:
        found = []
        for ago in range(SENSOR_LABEL_DAYS, 0, -1):
            found += _sensor_labels(room, int(TODAY // DAY) - ago, share)
        if ROOMS[room][1] > 1:
            found += _manual_room_labels(room)
        labels[f"area:{room}"] = sorted(found, key=lambda label: label["start"])
    return labels


def saved_data():
    """The JSON store's data for the home."""
    devices = {
        radar: {
            "entities": _entities(radar),
            "training_state": "unknown",
            "recording_enabled": room not in RECORDING_OFF,
            "history_labels": human_labels(room),
            "label_revision": 1,
            "histograms": _histograms(),
            "reference_profile": _reference_profile(radar, room),
            "history_cleanup": {"history_basis": "observed_seconds", "timestamp": NOW - 3600},
        }
        for radar, room in radars().items()
    }
    return {
        "devices": devices,
        "room_labels": room_labels(),
        "room_label_revision": {f"area:{room}": 3 for room in ROOMS},
        "storage_settings": dict(SETTINGS),
        "bermuda": {
            "devices": sorted(PHONES),
            "max_distance": None,
            "rooms": {
                f"area:{room}": {"max_distance": None} for room in ("living_room", "kitchen")
            },
        },
        "bermuda_migrated": True,
        "bermuda_targets_migrated": True,
        "bermuda_rooms_migrated": True,
    }


def build(root):
    """Write the home under ``root`` (a Home Assistant config folder)."""
    history = Path(root) / STORE_FOLDER / DOMAIN / "history"
    work = []
    for radar, room in radars().items():
        (history / daystore._folder_name(radar)).mkdir(parents=True)
        work += _write_days(history, radar, room)
        _partial_day(history, radar, room)
    # zlib lets go of the interpreter: this is the one slow step, and it runs in parallel. So
    # does the first open of each new file, which a virus scanner holds up for milliseconds.
    with ThreadPoolExecutor(os.cpu_count()) as pool:
        for _ in pool.map(lambda item: _publish(item[0], _day_file(*item[1:])), work):
            pass
    saved = {"version": 2, "minor_version": 2, "key": f"{DOMAIN}.data", "data": saved_data()}
    store = Path(root) / STORE_FOLDER / f"{DOMAIN}.data"
    store.write_text(json.dumps(saved, separators=(",", ":")), encoding="utf8")
    return Path(root)


def open_copy(template, target, *, whole=True):
    """A private copy of the home: finished days are linked, everything else copied. Unless
    ``whole``, the long-lived radars other than ``LONG_RADAR`` keep only today: the same saved
    metadata and registries, and a start that is not mostly opening files for a test's unread
    days. A test of what the size of the history does asks for the whole of it."""

    def place(source, destination):
        if not source.endswith(".day"):
            shutil.copy2(source, destination)
        elif whole or _kept_for_short_tests(Path(source).parent.name):
            os.link(source, destination)

    shutil.copytree(template, target, copy_function=place)
    return Path(target)


def _kept_for_short_tests(radar):
    return radar == LONG_RADAR or recorded_days(radars()[radar]) < RETAINED_DAYS

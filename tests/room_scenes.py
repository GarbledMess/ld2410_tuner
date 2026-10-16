"""Random radars, labels, guesses and rooms for room-truth and attribution tests."""

import random  # noqa: F401  (callers pass random.Random)

import test_tuner  # noqa: F401  (registers tuner_under_test)
from tuner_under_test.history import guesses
from tuner_under_test.rooms import attribution

NOW = 1_800_000_000.0


KEYS = ("g0_move", "g0_still", "g1_move")


def random_times(rng, count):
    """Sample times in order with recording gaps and the odd tie."""
    t, times = NOW + rng.choice((0, 0.25)), []
    for _ in range(count):
        t += rng.choice((6, 6, 6, 6, 6, 7, 0, 13, 40, 500))
        times.append(t)
    return times


def random_gates(rng, strong):
    """One sample's readings of the gates ``KEYS``: quiet, or loud on the gates that see."""
    gates = {}
    for key in KEYS:
        if rng.random() < 0.05:
            continue  # This gate did not read.
        gates[key] = rng.randint(30, 100) if strong and rng.random() < 0.6 else rng.randint(0, 9)
    return gates


def random_rows(rng, count=300):
    """Rows ``(time, gates, label)`` of a radar that is loud in stretches."""
    times, loud, rows = random_times(rng, count), False, []
    for t in times:
        if rng.random() < 0.05:
            loud = not loud
        rows.append((t, random_gates(rng, loud), "unknown"))
    return rows


def steps(rng, times, answers):
    """A reader that is constant between random change points, like every room reader."""
    edges = sorted(rng.choice(times) + rng.choice((0, 0.5, -0.5)) for _ in range(len(times) // 12))
    values = [rng.choice(answers) for _ in range(len(edges) + 1)]

    def read(t):
        return values[sum(1 for edge in edges if edge <= t)]

    return read


TRUTHS = (
    None,
    ("present", "human", 1.0),
    ("present", "automatic", 0.8),
    ("not_present", "human", 1.0),
    ("not_present", "estimate", 0.7),
    ("present", "human", 1.0),
    ("not_present", "human", 1.0),
)


LABELS = (None, None, None, "present", "not_present", "unknown")


def scenario(rng, count=300, groups=None):
    rows = random_rows(rng, count)
    times = [row[0] for row in rows]
    own = steps(rng, times, LABELS)
    truths = {
        name: steps(rng, times, TRUTHS) for name in (groups or rng.choice((("g",), ("g", "h"))))
    }
    return rows, times, own, truths


def random_runs(rng, times, step=6):
    """Guess runs over the span of ``times``: stretches of one guess with gaps between."""
    runs, t, end = [], times[0], times[-1]
    while t < end:
        length = rng.choice((6, 30, 120, 600))
        if rng.random() < 0.8:
            level = rng.choice((8, 10, 11, 12, 16, 20))  # Steps of 5 %: under 55 % is no guess.
            runs.append(
                (t, t + length, rng.choice(("present", "not_present")), level, guesses.REFERENCE)
            )
        t += length + rng.choice((0, 0, 6, 60))
    return runs


def random_label(rng, times, source):
    start = rng.choice(times) + rng.choice((0, 0.5))
    state = rng.choice(("present", "not_present", "unknown"))
    label = {
        "start": start,
        "end": start + rng.choice((12, 90, 700)),
        "state": state,
        "source": source,
    }
    if source == "sensor":
        label["confidence"] = rng.choice((0.4, 0.6, 0.9))
    return label


def scene(rng):
    """Random room inputs: sample times, a room snapshot of 1-3 members with labels
    and live training, room labels, and each member's recorded guesses."""
    times = random_times(rng, rng.choice((30, 300)))
    members = ["a", "b", "c"][: rng.choice((1, 2, 3))]
    devices = {}
    for name in members:
        device = {}
        if rng.random() < 0.5:
            device["history_labels"] = [
                random_label(rng, times, "manual") for _ in range(rng.randrange(1, 4))
            ]
        if rng.random() < 0.3:
            device["training_state"] = rng.choice(("present", "not_present"))
            device["training_label_start"] = rng.choice(times)
            device["training_expires_at"] = rng.choice((None, rng.choice(times) + 100))
        devices[name] = device
    zone = {"name": "Zone", "area_id": "x", "kind": "zone", "device_ids": members[:1]}
    area = {"name": "Area", "area_id": "x", "kind": "area", "device_ids": members}
    labels = {
        "zone:z": [random_label(rng, times, rng.choice(("manual", "sensor"))) for _ in range(3)],
        "area:x": [random_label(rng, times, rng.choice(("manual", "sensor"))) for _ in range(3)],
    }
    saved = {
        "device_id": members[0],
        "own": ["zone:z", "area:x"],
        "groups": {"zone:z": zone, "area:x": area},
        "room_labels": labels,
        "members": {name: attribution.label_fields(device) for name, device in devices.items()},
    }
    others = {name: guesses.Guesses(random_runs(rng, times)) for name in members}
    return times, saved, others


def room_label(members, timestamp):
    """The room's label from its members' evidence at ``timestamp``, as room truth derives
    it from member labels (``evidence.combine`` of each ``device_label``)."""
    from tuner_under_test.rooms import evidence

    return evidence.combine([evidence.device_label(item, timestamp) for item in members.values()])

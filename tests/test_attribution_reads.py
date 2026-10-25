"""Attribution reads each truth once per stretch between its edges, exactly as per cell.

Every reader attribution uses (a radar's own labels, its groups' room truth, the estimate
from member guesses) can change its answer only at the edges the cells are cut at, so reading
one cell of each stretch answers for all of them. ``reference`` is the per-cell reading.
"""

import math
import random
from bisect import bisect_left, bisect_right
from math import ceil
from unittest.mock import patch

from test_rooms import member
from tuner_under_test.calibration.constants import SAMPLE_SECONDS
from tuner_under_test.const import HISTORY_KEYS
from tuner_under_test.history import guesses, spans
from tuner_under_test.presence.inference import MIN_REFERENCE
from tuner_under_test.rooms import attribution, event_attribution

STATES = ("present", "not_present", "unknown")


def reference(runtime, device_id, edges, reads, own):
    """The attribution as each cell reads every truth itself (before reads were shared)."""
    cells = []
    physical = spans.events(
        runtime,
        device_id,
        start=getattr(runtime, "_attribution_since", 0) or -math.inf,
        end=getattr(runtime, "_attribution_until", math.inf),
    )
    for cell in physical:
        cuts = [cell.start, *edges[bisect_right(edges, cell.start) : bisect_left(edges, cell.end)]]
        cuts.append(cell.end)
        cells += [(low, high, cell.values) for low, high in zip(cuts, cuts[1:], strict=False)]
    quiet = attribution.quiet_reader(own, reads)
    bins = [[0.0] * 101 for _ in HISTORY_KEYS]
    for low, high, values in cells:
        if quiet(low):
            for histogram, value in zip(bins, values, strict=True):
                if value is not None:
                    histogram[ceil(value)] += high - low
    baseline = {
        i: event_attribution.Quiet([seconds / SAMPLE_SECONDS for seconds in histogram])
        for i, histogram in enumerate(bins)
        if sum(histogram) >= MIN_REFERENCE * SAMPLE_SECONDS
    }
    found = {}
    for group, read in reads.items():
        episodes, current = [], []
        for cell in cells:
            low, _high, values = cell
            label = read(low)
            present = any(v is not None for v in values) and label and label[0] == "present"
            if current and (not present or current[-1][1] != low):
                episodes.append(current)
                current = []
            if present:
                current.append(cell)
        if current:
            episodes.append(current)
        found[group] = [
            (
                (episode[0][0], event_attribution._closed(episode[-1][1])),
                event_attribution._decision({own(c[0]) for c in episode}, episode, baseline),
            )
            for episode in episodes
        ]
    return cells, found


def scenario(seed):
    """Two radars of a zone, one also in a room: random readings (some missing, with gaps),
    own and room labels with edges on and between sample times, and member guess runs."""
    rnd = random.Random(seed)
    span = 6 * 3600

    def labels(count):
        found, t = [], 0.0
        for _ in range(count):
            start = t + rnd.choice((0, 6, 6 * rnd.randint(1, 40), rnd.uniform(0, 300)))
            end = start + rnd.choice((6, 60, 6 * rnd.randint(2, 300), rnd.uniform(1, 900)))
            found.append({"start": start, "end": end, "state": rnd.choice(STATES)})
            t = end
        return found

    def values(count):
        return [rnd.choice((0, 2, 3, 5, 8, 40, 61, 99, 255)) for _ in range(count)]

    items = {key: member(key, values(span // 6), labels(25)) for key in ("a", "b")}
    for key, item in items.items():
        item["view"].data["devices"][key]["history_labels"] = labels(25)
    runs = {
        key: guesses.Guesses(
            sorted(
                (start, start + rnd.choice((6, 120, 1800)), rnd.choice(STATES[:2]), level, basis)
                for start, level, basis in (
                    (6 * rnd.randint(0, span // 6), rnd.randint(50, 100), "radar")
                    for _ in range(30)
                )
            )
        )
        for key in items
    }
    room_labels = {
        group: [{**label, "source": "manual"} for label in labels(20)] for group in ("zone", "room")
    }
    saved = {
        "device_id": "a",
        "own": ["zone", "room"],
        "groups": {
            "zone": {"device_ids": ["a", "b"], "kind": "zone"},
            "room": {"device_ids": ["a"], "kind": "room"},
        },
        "members": {key: item["view"].data["devices"][key] for key, item in items.items()},
        "room_labels": room_labels,
    }
    return items["a"]["view"], saved, runs


def test_shared_reads_attribute_exactly_as_reading_every_cell():
    for seed in range(12):
        view, saved, runs = scenario(seed)
        for human, others in ((True, None), (False, runs)):
            found = event_attribution.calculate(view, "a", saved, human, others)["found"]
            view.__dict__.pop("_native_found_job", None)  # A job's view keeps its answers.
            with patch.object(event_attribution, "_found", side_effect=lambda *a: reference(*a)[1]):
                expected = event_attribution.calculate(view, "a", saved, human, others)["found"]
            view.__dict__.pop("_native_found_job", None)
            assert found == expected, (seed, human)
            assert any(found.values()), "episodes are found"

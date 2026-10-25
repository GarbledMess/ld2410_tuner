"""Room attribution made a day at a time equals attribution over the whole window.

Several recorded days with episodes running over midnight, a window starting within a day,
the day being recorded, two groups, human and full truth: per-day parts joined across days
must give the episodes, attributions and confidences ``event_attribution`` gives from all
the cells at once; finished days' parts are reused while their readers answer alike.
"""

import math
import random
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import test_tuner as harness
from test_attribution_reads import reference
from tuner_under_test.history import day_results, guesses
from tuner_under_test.rooms import attribution_days, event_attribution

START = 1_760_486_400.0  # A UTC midnight.
DAY = 86_400
STATES = ("present", "not_present", "unknown")


def history(seed, days=3, live=True):
    """Radar "a" (and member "b"'s labels) over ``days`` days, readings held in stretches so
    some stay above the baseline for minutes, with gaps; the last day still recorded."""
    rnd = random.Random(seed)
    view = SimpleNamespace(data={"devices": {}}, _history_runtime={})
    t, end = START + rnd.uniform(0, 3000), START + days * DAY - 600
    while t < end:
        stretch = []
        for _ in range(rnd.randint(20, 400)):
            level = rnd.choice((None, 0, 2, 3, 8, 40, 61, 99))
            for _ in range(rnd.randint(1, 12)):
                stretch.append((t, [level if rnd.random() < 0.9 else None] * 18))
                t += rnd.choice((6, 6, 6, 30))
        last = len(stretch) and t >= end and live
        if last:
            harness.recorded(view, "a", stretch, None, open_until=t)
        else:
            harness.recorded(view, "a", stretch, t)
        t += rnd.choice((0, 60, 900))
    view._days.flush(now=view._history_until)
    return view


def labels(rnd, count, span):
    found, t = [], START
    for _ in range(count):
        start = t + rnd.choice((0, 6, rnd.uniform(0, 7200)))
        end = start + rnd.choice((60, 3600, rnd.uniform(600, 20_000)))
        found.append({"start": start, "end": min(end, START + span), "state": rnd.choice(STATES)})
        t = end
    return found


def scenario(seed, days=3, live=True):
    rnd = random.Random(seed + 1000)
    view = history(seed, days, live)
    span = days * DAY
    own = {"history_labels": labels(rnd, 12, span), "training_state": "unknown"}
    other = {"history_labels": labels(rnd, 12, span), "training_state": "unknown"}
    view.data["devices"] = {"a": own, "b": other}
    room = {g: [{**x, "source": "manual"} for x in labels(rnd, 10, span)] for g in ("zone", "room")}
    saved = {
        "device_id": "a",
        "own": ["zone", "room"],
        "groups": {
            "zone": {"device_ids": ["a", "b"], "kind": "zone"},
            "room": {"device_ids": ["a"], "kind": "room"},
        },
        "members": {"a": own, "b": other},
        "room_labels": room,
    }
    runs = {
        key: guesses.Guesses(
            sorted(
                (
                    s,
                    s + rnd.choice((60, 1800, 7200)),
                    rnd.choice(STATES[:2]),
                    rnd.randint(50, 99),
                    0,
                )
                for s in sorted(START + rnd.uniform(0, span) for _ in range(20))
            )
        )
        for key in ("a", "b")
    }
    return view, saved, runs


def both(view, saved, human, others):
    """``found`` from the whole window's cells, read cell by cell (``reference``), and as
    attribution makes it from per-day parts, for the same inputs."""
    captured = {}
    real = event_attribution._found

    def capture(*args):
        captured["args"] = args
        return real(*args)

    view.__dict__.pop("_native_found_job", None)
    with patch.object(event_attribution, "_found", side_effect=capture):
        days = event_attribution.calculate(view, "a", saved, human, others)["found"]
    view.__dict__.pop("_native_found_job", None)
    return reference(*captured["args"])[1], days


def same(found, expected):
    assert list(found) == list(expected)
    for group in expected:
        assert len(found[group]) == len(expected[group]), group
        for (span, decided), (want_span, wanted) in zip(found[group], expected[group], strict=True):
            assert span == want_span
            assert (decided["state"], decided["inferred"]) == (wanted["state"], wanted["inferred"])
            assert decided["confidence"] == pytest.approx(wanted["confidence"], abs=1e-12)


@pytest.mark.parametrize("seed", range(10))
def test_days_joined_attribute_as_the_whole_window(seed):
    day_results.clear()
    view, saved, runs = scenario(seed)
    episodes = 0
    for since in (None, START + DAY / 3):  # The whole history, or a window within a day.
        view._attribution_since = since or 0
        for human, others in ((True, None), (False, runs)):
            whole, days = both(view, saved, human, others)
            episodes += sum(map(len, whole.values()))
            same(days, whole)
    assert episodes > 3, "episodes are found"


def test_episodes_over_midnight_are_one_episode():
    day_results.clear()
    found = 0
    for seed in range(10):
        view, saved, runs = scenario(seed)
        whole, days = both(view, saved, False, runs)
        same(days, whole)
        found += sum(
            1
            for items in whole.values()
            for (start, end), _ in items
            if math.floor((start - START) / DAY) != math.floor((end - START) / DAY)
        )
    assert found, "the scenarios hold episodes running over midnight"


def test_finished_days_are_reused_until_their_readers_change():
    day_results.clear()
    view, saved, runs = scenario(3)
    measured = []
    real = attribution_days._measure

    def measure(runtime, device_id, low, high, *args):
        measured.append((low, high))
        return real(runtime, device_id, low, high, *args)

    with patch.object(attribution_days, "_measure", side_effect=measure):
        first, _ = both(view, saved, True, None)
        count = len(measured)
        measured.clear()
        again, _ = both(view, saved, True, None)
        assert len(measured) == 1, "only the day being recorded is measured again"
        assert again == first
        measured.clear()
        label = view.data["devices"]["a"]["history_labels"][0]
        label["state"] = "not_present" if label["state"] != "not_present" else "present"
        both(view, saved, True, None)
    assert 1 < len(measured) < count, "a changed label re-measures the days it is in"


def test_a_stretch_sustained_only_over_midnight_attributes_the_episode():
    """20 s above the baseline either side of midnight: only joined do they last the sustain
    time, so only the joined runs attribute the episode."""
    day_results.clear()
    view = SimpleNamespace(data={"devices": {}}, _history_runtime={})
    midnight = START + DAY
    quiet = [(START + 600 + 6 * i, [1] * 18) for i in range(600)]  # An hour of quiet readings.
    harness.recorded(view, "a", quiet, START + 600 + 3600)
    around = [
        (
            midnight - 600 + 2 * i,
            [99 if abs(midnight - 600 + 2 * i - midnight + 1) <= 20 else 0] * 18,
        )
        for i in range(600)
    ]
    harness.recorded(view, "a", around, midnight + 600)
    view._days.flush(now=midnight + 2 * DAY)
    device = {"history_labels": [], "training_state": "unknown"}
    view.data["devices"] = {"a": device}
    room = [
        {"start": START, "end": START + 5000, "state": "not_present", "source": "manual"},
        {"start": midnight - 600, "end": midnight + 600, "state": "present", "source": "manual"},
    ]
    saved = {
        "device_id": "a",
        "own": ["zone"],
        "groups": {"zone": {"device_ids": ["a"], "kind": "zone"}},
        "members": {"a": device},
        "room_labels": {"zone": room},
    }
    whole, days = both(view, saved, True, None)
    assert [decided["state"] for _span, decided in whole["zone"]] == ["yes"]
    same(days, whole)

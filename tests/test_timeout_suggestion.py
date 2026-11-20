"""A radar timeout suggested from the radar's own history: the one whose missed labelled
presence and false light, priced as Learn prices them, cost least."""

import pytest
import test_tuner
from small_views import view
from tuner_under_test.calibration import timeout_suggestion
from tuner_under_test.engine.hybrid.constants import PRESENCE_PRICE

BASE = 1_700_000_000.0
HOUR = 3600.0
THRESHOLDS = {"g0_move": 20}


def _reading(value):
    return (value,) + (0,) * 17


def _history(dip=3, blips_every=600, hours=2):
    """``hours`` of presence reading 50 each second, dropping to 0 for ``dip`` seconds every
    minute (mid-minute); then ``hours`` empty, reading 0 with a one-second 50 every ``blips_every`` s."""
    events, t = [], BASE
    for second in range(int(hours * HOUR)):
        events.append((t + second, _reading(0 if 30 <= second % 60 < 30 + dip else 50)))
    t += hours * HOUR
    for second in range(int(hours * HOUR)):
        events.append((t + second, _reading(50 if second % blips_every == 300 else 0)))
    end = t + hours * HOUR
    labels = [
        {"start": BASE, "end": BASE + hours * HOUR, "state": "present"},
        {"start": BASE + hours * HOUR, "end": end, "state": "not_present"},
    ]
    return view([(events, end)], labels)


def test_the_shortest_timeout_that_bridges_the_dips_is_suggested():
    found = timeout_suggestion.suggest(_history(dip=3), "any-name", THRESHOLDS)
    assert found["timeout"] == 3.0
    rows = {row["timeout"]: row for row in found["candidates"]}
    assert rows[1.0]["dark_spells"] == 120 and rows[3.0]["dark_spells"] == 0
    assert rows[3.0]["false_starts"] == 12
    assert rows[5.0]["false_seconds"] > rows[3.0]["false_seconds"]


def test_noise_that_costs_more_than_the_dips_keeps_the_timeout_short():
    # A blip every 20 seconds of empty time: bridging one-second dips costs far more false
    # light than the presence it saves.
    found = timeout_suggestion.suggest(_history(dip=1, blips_every=20), "any-name", THRESHOLDS)
    assert found["timeout"] == 1.0


def test_without_labelled_presence_there_is_nothing_to_suggest():
    source = _history()
    source.data["devices"]["any-name"]["history_labels"] = [
        label
        for label in source.data["devices"]["any-name"]["history_labels"]
        if label["state"] != "present"
    ]
    assert timeout_suggestion.suggest(source, "any-name", THRESHOLDS) is None


def test_a_gate_without_a_threshold_detects_nothing():
    found = timeout_suggestion.suggest(_history(), "any-name", {"g1_move": 20})
    rows = {row["timeout"]: row for row in found["candidates"]}
    assert rows[1.0]["missed_seconds"] == 2 * HOUR


def test_labelled_time_the_radar_did_not_record_is_not_missed_presence():
    source = _history()
    labels = source.data["devices"]["any-name"]["history_labels"]
    labels.insert(0, {"start": BASE - 10 * HOUR, "end": BASE - 5 * HOUR, "state": "present"})
    found = timeout_suggestion.suggest(source, "any-name", THRESHOLDS)
    rows = {row["timeout"]: row for row in found["candidates"]}
    assert rows[3.0]["missed_seconds"] == 0
    assert found["timeout"] == 3.0


def test_the_suggestion_runs_as_background_work():
    from tuner_under_test.worker import priority

    assert priority.is_background("timeout_suggestion")
    assert not priority.is_background("comparison")


def test_a_discarded_empty_guess_is_not_empty_time():
    from tuner_under_test.history import guesses

    assert not guesses.counted_empty((0, 10, "not_present", 0, guesses.DISCARDED))
    assert guesses.counted_empty((0, 10, "not_present", 15, guesses.REFERENCE))


def test_the_radar_s_own_timeout_is_always_a_candidate():
    found = timeout_suggestion.suggest(_history(dip=3), "any-name", THRESHOLDS, current=4.0)
    assert [row["timeout"] for row in found["candidates"]] == [1, 2, 3, 4, 5, 8, 12, 15, 20, 30]
    assert found["timeout"] == 3.0


def test_a_cheaper_timeout_is_written():
    from tuner_under_test.calibration import timeout_apply

    suggestion = {
        "timeout": 3.0,
        "candidates": [{"timeout": 1.0, "cost": 50.0}, {"timeout": 3.0, "cost": 10.0}],
    }
    assert timeout_apply.decision(suggestion, 1.0) == 3.0


def test_the_radar_s_timeout_is_kept_when_it_is_the_suggestion():
    from tuner_under_test.calibration import timeout_apply

    best = {
        "timeout": 3.0,
        "candidates": [{"timeout": 1.0, "cost": 9}, {"timeout": 3.0, "cost": 5}],
    }
    assert timeout_apply.decision(best, 3.0) is None


def test_a_shorter_timeout_that_works_as_well_replaces_a_longer_one():
    from tuner_under_test.calibration import timeout_apply

    tie = {"timeout": 1.0, "candidates": [{"timeout": 1.0, "cost": 5}, {"timeout": 3.0, "cost": 5}]}
    assert timeout_apply.decision(tie, 3.0) == 1.0


def test_the_shortest_timeout_keeping_the_chosen_share_of_the_improvement_is_suggested():
    # Three-second dips: two seconds bridges about half the missed time, three bridges it all.
    rows = lambda keep: timeout_suggestion.suggest(  # noqa: E731
        _history(dip=3), "any-name", THRESHOLDS, keep=keep
    )
    assert rows(0.4)["timeout"] == 2.0
    assert rows(0.8)["timeout"] == 3.0
    assert rows(1.0)["timeout"] == rows(1.0)["cheapest"] == 3.0


def test_without_any_improvement_the_shortest_timeout_is_suggested():
    found = timeout_suggestion.suggest(_history(), "any-name", {"g1_move": 20}, keep=0.8)
    assert found["timeout"] == 1.0


def test_an_improvement_within_the_noise_keeps_the_shortest_timeout():
    rows = [
        {"timeout": 1.0, "cost": 1000.0},
        {"timeout": 3.0, "cost": 975.0},  # 2.5% cheaper: noise, not a reason to hold longer.
        {"timeout": 8.0, "cost": 990.0},
    ]
    assert timeout_suggestion._chosen(rows, 0.8)["timeout"] == 1.0
    rows[1]["cost"] = 960.0  # 4% cheaper: a real improvement.
    assert timeout_suggestion._chosen(rows, 0.8)["timeout"] == 3.0


def test_without_a_suggestion_or_a_reported_timeout_nothing_is_written():
    from tuner_under_test.calibration import timeout_apply

    best = {
        "timeout": 3.0,
        "candidates": [{"timeout": 1.0, "cost": 9}, {"timeout": 3.0, "cost": 5}],
    }
    assert timeout_apply.decision(None, 1.0) is None
    assert timeout_apply.decision(best, None) is None


LOUNGE = {"area:lounge": {"kind": "area", "area_id": "lounge", "device_ids": ["a", "b"]}}
ROOM_LABELS = {
    "area:lounge": [
        {"start": BASE, "end": BASE + 2 * HOUR, "state": "present", "source": "manual"},
        {
            "start": BASE + 2 * HOUR,
            "end": BASE + 4 * HOUR,
            "state": "not_present",
            "source": "manual",
        },
    ]
}


def _lounge_radar(device_id, present_value, dip):
    """A radar of the two-radar lounge (labelled only as a room): two hours of presence
    reading ``present_value`` with a ``dip``-second drop every minute, then two empty hours
    with a one-second blip every ten minutes."""
    from test_rooms import member

    events = []
    for second in range(int(2 * HOUR)):
        dark = 30 <= second % 60 < 30 + dip
        events.append((BASE + second, _reading(0 if dark else present_value)))
    for second in range(int(2 * HOUR)):
        events.append((BASE + 2 * HOUR + second, _reading(50 if second % 600 == 300 else 0)))
    result = member(device_id, [], [])["view"]
    test_tuner.recorded(result, device_id, events, BASE + 4 * HOUR)
    result.hass = None
    result.data["room_snapshot"] = {
        "device_id": device_id,
        "own": ["area:lounge"],
        "groups": LOUNGE,
        "room_labels": ROOM_LABELS,
        "members": {key: {"history_labels": []} for key in ("a", "b")},
    }
    return result


def _lounge(b_value, b_dip):
    views = {"a": _lounge_radar("a", 50, 3), "b": _lounge_radar("b", b_value, b_dip)}
    return views, dict.fromkeys(views, THRESHOLDS), dict.fromkeys(views, 1.0)


def test_a_shared_room_s_presence_is_missed_only_where_none_of_its_radars_sees_it():
    # The lounge's other radar reads nothing: the room's presence is this radar's to bridge.
    views, thresholds, current = _lounge(b_value=0, b_dip=0)
    found = timeout_suggestion.suggest_room(views, ["a"], thresholds, current)["a"]
    rows = {row["timeout"]: row for row in found["candidates"]}
    assert rows[1.0]["dark_spells"] == 120 and rows[3.0]["dark_spells"] == 0
    assert found["timeout"] == 3.0


def test_a_radar_whose_room_mate_covers_the_room_keeps_its_timeout_short():
    views, thresholds, current = _lounge(b_value=50, b_dip=0)
    found = timeout_suggestion.suggest_room(views, ["a"], thresholds, current)["a"]
    rows = {row["timeout"]: row for row in found["candidates"]}
    assert rows[1.0]["missed_seconds"] == 0
    assert found["timeout"] == 1.0


def test_a_room_learn_tunes_its_radars_together():
    # Both radars drop out at the same moments: one of them bridging the dips lights the room.
    views, thresholds, current = _lounge(b_value=50, b_dip=3)
    found = timeout_suggestion.suggest_room(views, ["a", "b"], thresholds, current)
    assert sorted(found[key]["timeout"] for key in ("a", "b")) == [1.0, 3.0]


def test_a_radar_without_any_labelled_presence_gets_no_suggestion_in_a_room():
    views, thresholds, current = _lounge(b_value=50, b_dip=0)
    for item in views.values():
        item.data["room_snapshot"]["room_labels"] = {"area:lounge": ROOM_LABELS["area:lounge"][1:]}
    found = timeout_suggestion.suggest_room(views, ["a", "b"], thresholds, current)
    assert found == {"a": None, "b": None}


def _costs(found):
    return {row["timeout"]: (row["cost"], row["missed_seconds"]) for row in found["candidates"]}


def test_a_radar_s_presence_priority_scales_the_price_of_its_missed_presence():
    history = _history(dip=3)
    default = _costs(timeout_suggestion.suggest(history, "any-name", THRESHOLDS))
    priorities = {"radars": {"any-name": 4.0}}
    raised = _costs(
        timeout_suggestion.suggest(history, "any-name", THRESHOLDS, priorities=priorities)
    )
    assert default[1.0][1] > 0
    for timeout, (cost, missed) in default.items():
        assert raised[timeout][0] == pytest.approx(cost + 3 * PRESENCE_PRICE * missed)


def test_a_shared_room_s_priority_prices_the_room_s_missed_presence():
    views, thresholds, current = _lounge(b_value=0, b_dip=0)
    default = timeout_suggestion.suggest_room(views, ["a"], thresholds, current)["a"]
    priorities = {"rooms": {"area:lounge": 0.1}, "radars": {"b": 5.0}}
    lowered = timeout_suggestion.suggest_room(
        views, ["a"], thresholds, current, priorities=priorities
    )["a"]
    for timeout, (cost, missed) in _costs(default).items():
        assert _costs(lowered)[timeout][0] == pytest.approx(cost - 0.9 * PRESENCE_PRICE * missed)


def test_a_zone_and_its_area_labelled_present_together_count_each_second_once():
    # The sofa zone and the lounge both labelled present over the same two hours: a second
    # the radars miss is one missed second, not one for the zone and one for the area.
    views, thresholds, current = _lounge(b_value=0, b_dip=0)
    alone = _costs(timeout_suggestion.suggest_room(views, ["a"], thresholds, current)["a"])
    sofa = {"kind": "zone", "area_id": "lounge", "device_ids": ["a", "b"]}
    for item in views.values():
        snapshot = item.data["room_snapshot"]
        snapshot["groups"] = {**LOUNGE, "zone:sofa": sofa}
        snapshot["own"] = ["area:lounge", "zone:sofa"]
        snapshot["room_labels"] = {**ROOM_LABELS, "zone:sofa": ROOM_LABELS["area:lounge"]}
    found = timeout_suggestion.suggest_room(views, ["a"], thresholds, current)["a"]
    assert alone[1.0][1] > 0
    assert _costs(found) == {key: pytest.approx(value) for key, value in alone.items()}
    assert {row["timeout"]: row["dark_spells"] for row in found["candidates"]}[1.0] == 120


def test_a_second_missed_in_two_rooms_is_priced_at_the_higher_priority():
    views, thresholds, current = _lounge(b_value=0, b_dip=0)
    alone = _costs(
        timeout_suggestion.suggest_room(
            views, ["a"], thresholds, current, priorities={"rooms": {"area:lounge": 4.0}}
        )["a"]
    )
    sofa = {"kind": "zone", "area_id": "lounge", "device_ids": ["a", "b"]}
    for item in views.values():
        snapshot = item.data["room_snapshot"]
        snapshot["groups"] = {**LOUNGE, "zone:sofa": sofa}
        snapshot["own"] = ["area:lounge", "zone:sofa"]
        snapshot["room_labels"] = {**ROOM_LABELS, "zone:sofa": ROOM_LABELS["area:lounge"]}
    found = timeout_suggestion.suggest_room(
        views, ["a"], thresholds, current, priorities={"rooms": {"area:lounge": 4.0}}
    )["a"]
    assert _costs(found) == {key: pytest.approx(value) for key, value in alone.items()}

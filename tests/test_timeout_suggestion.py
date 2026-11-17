"""A radar timeout suggested from the radar's own history: the one whose missed labelled
presence and false light, priced as Learn prices them, cost least."""

import test_tuner  # noqa: F401
from small_views import view
from tuner_under_test.calibration import timeout_suggestion

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


def test_without_a_suggestion_or_a_reported_timeout_nothing_is_written():
    from tuner_under_test.calibration import timeout_apply

    best = {
        "timeout": 3.0,
        "candidates": [{"timeout": 1.0, "cost": 9}, {"timeout": 3.0, "cost": 5}],
    }
    assert timeout_apply.decision(None, 1.0) is None
    assert timeout_apply.decision(best, None) is None

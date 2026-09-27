"""Elapsed time, density invariance and review attribution regressions."""

import copy

import pytest
import test_tuner as harness
from tuner_under_test.calibration.duration import DurationGroup, DurationReplay
from tuner_under_test.calibration.influence import period_influence
from tuner_under_test.calibration.timing_metrics import evaluate


def rows(times, label="present", energy=30, confidence=None):
    result = [(t, {"g0_still": energy}, label) for t in times]
    return [(*r, confidence) for r in result] if confidence is not None else result


def bits(values):
    return sum(1 << i for i, value in enumerate(values) if value)


def test_dense_fluctuations_do_not_outweigh_duration():
    sparse = [0, 5, 10, 15, 20]
    dense = [0, 5, 10, 10.1, 10.2, 10.3, 10.4, 10.5, 15, 20]
    # Equal transition boundaries, many extra readings inside an active period.
    a = DurationGroup(rows(sparse)).measure(bits([0, 1, 1, 1, 0]))
    b = DurationGroup(rows(dense)).measure(bits([0, 1, 1, 1, 1, 1, 1, 1, 1, 0]))
    assert a["active_seconds"] == b["active_seconds"] == 15
    assert a["observed_seconds"] == b["observed_seconds"] == 20
    assert DurationGroup(rows(sparse)).recent_start == DurationGroup(rows(dense)).recent_start == 16


def test_five_second_burst_is_not_five_minutes_and_gaps_are_not_evidence():
    times = list(range(301)) + list(range(10000, 10301))
    group = DurationGroup(rows(times))
    brief = group.measure(bits([10 <= t < 15 for t in times]))
    sustained = group.measure(bits([t < 301 for t in times]))
    assert brief["active_seconds"] == 5
    assert sustained["active_seconds"] == 300
    assert brief["observed_seconds"] == 600
    assert brief["events"] == 1


def test_union_of_gates_counts_false_active_time_only_once():
    groups = {"present": rows(range(100)), "not_present": rows(range(200, 300), "not_present", 0)}
    for i, row in enumerate(groups["not_present"]):
        row[1].update(g0_still=30 if 10 <= i < 20 else 0, g1_still=30 if 15 <= i < 25 else 0)
    measured = evaluate(groups, {"g0_still": 10, "g1_still": 10})["duration"]
    assert measured["false_positive_seconds"] == 15
    assert measured["false_positive_score"] == pytest.approx(-100 * 15 / 99)
    assert measured["presence_recall"] == 1


def test_timeout_extends_false_activity_and_sparse_spikes_are_not_discarded():
    data = rows(range(0, 31, 6), "not_present")
    raw = DurationGroup(data, absent=True).measure(4)
    timed = DurationGroup(data, {"timeout": 1}, absent=True).measure(4)
    assert raw["active_seconds"] == 6
    assert timed["active_seconds"] == pytest.approx(13, abs=1e-5)


def test_sparse_recovery_has_onset_uncertainty_even_without_delayed_on():
    data = rows(range(0, 31, 6))
    replay = DurationReplay(data, [], {"timeout": 1})
    report = replay.summary(bits([1, 1, 0, 1, 1, 1]), 0)
    assert report["missed_seconds"] == pytest.approx(5, abs=1e-5)
    assert report["missed_seconds_upper"] == 11
    assert report["presence_recall_lower"] < report["presence_recall"]


def test_singletons_and_hold_consumed_sessions_have_no_invented_duration():
    group = DurationGroup(rows([0, 100]), {"timeout": 1})
    result = group.measure(3)
    assert result["observed_seconds"] == 0
    assert result["unscored_samples"] == 2
    assert DurationReplay([], []).summary(0, 0)["presence_recall"] is None


def test_automatic_confidence_changes_time_weight_not_human_priority():
    low = rows(range(11), confidence=0.2) + rows(range(100, 111), confidence=0.9)
    group = DurationGroup(low)
    report = group.measure((1 << 11) - 1)
    assert report["weighted_seconds"] == pytest.approx(11)
    assert report["weighted_active_seconds"] == pytest.approx(2)
    human = harness.row_samples({"g0_still": 30}, {"g0_still": 10})
    auto = rows(range(1000, 2000), energy=5, confidence=0.99)
    fit = harness.fit(human, ["g0_still"], automatic=auto)
    assert fit["training"]["duration"]["presence_recall"] == 1
    assert fit["training"]["duration"]["false_positive_seconds"] == 0


def test_influence_identifies_bad_chunk_without_deleting_or_claiming_refit():
    groups = {
        "present": rows(range(100)),
        "not_present": rows(range(200, 300), "not_present", 30)
        + rows(range(400, 500), "not_present", 0),
    }
    original = copy.deepcopy(groups)
    report = period_influence(groups, {"g0_still": 10})
    assert len(report) == 1
    bad = report[0]
    assert (bad["start"], bad["end"]) == (200, 299)
    assert bad["error_share_percent"] == 100
    assert bad["score_without_period"] == 0
    assert bad["comparison"] == "same_thresholds_not_refitted"
    assert bad["excluded_automatically"] is False
    assert groups == original
    groups["not_present"] = groups["not_present"][:100]
    assert period_influence(groups, {"g0_still": 10})[0]["score_without_period"] is None


def test_unknown_timeout_does_not_apply_known_filters_or_warmup():
    data = rows(range(11))
    raw = DurationGroup(data).measure(1023)
    unknown = DurationGroup(data, {"timeout": None, "on_delay": 5, "off_delay": 20}).measure(1023)
    assert unknown == raw


def test_automatic_time_target_precedes_false_positive_penalty():
    replay = DurationReplay(rows(range(100)), rows(range(200, 300), "not_present"))
    all_hits = (1 << 100) - 1
    # Meeting the target with a short false event beats missing a whole second.
    assert replay.automatic_rank(all_hits, 1 << 20) < replay.automatic_rank(all_hits ^ (1 << 20), 0)


def test_isolated_human_presence_still_overrides_long_confident_guesses():
    # No invented seconds for the human point, but guesses cannot overrule it.
    human = [(0, {"g0_still": 20}, "present")]
    automatic = rows(range(100, 200), energy=30, confidence=0.99)
    automatic += rows(range(300, 400), "not_present", energy=25, confidence=0.99)
    result = harness.fit(human, ["g0_still"], automatic=automatic)
    assert result["proposals"]["g0_still"]["threshold"] < 20
    assert result["training"]["duration"]["present_seconds"] == 0
    assert result["training"]["duration"]["unscored_presence_samples"] == 1
    assert result["status"] != "ok"


@pytest.mark.parametrize("rank_method", ["rank", "automatic_rank"])
def test_user_tradeoffs_have_finite_presence_priority(rank_method):
    replay = DurationReplay(rows(range(6000)), rows(range(10000, 16000), "not_present"))
    rank = getattr(replay, rank_method)
    all_hits = (1 << 6000) - 1
    missed22 = ((1 << 22) - 1) << 100
    false3 = 7 << 100
    # Three seconds of false activity do not buy 22 seconds of missed presence.
    assert rank(all_hits, false3) < rank(all_hits ^ missed22, 0)
    # Twenty distinct real activations are not worth avoiding a one-second miss.
    false20 = sum(1 << (100 + i * 10) for i in range(20))
    assert replay.summary(all_hits, false20)["false_trigger_events"] == 20
    assert rank(all_hits ^ (1 << 100), 0) < rank(all_hits, false20)
    # Both recalls exceed 99.9%: no free budget to trade two more misses for 3s noise.
    assert replay.summary(all_hits ^ false3, 0)["presence_recall"] > 0.999
    assert rank(all_hits ^ (1 << 100), false3) < rank(all_hits ^ false3, 0)


@pytest.mark.parametrize(
    "config", [{"timeout": 3}, {"timeout": 1, "on_delay": 0.5, "off_delay": 3}]
)
def test_timeout_or_off_delay_covered_dip_can_reduce_false_activity(config):
    replay = DurationReplay(rows(range(40)), rows(range(100, 140), "not_present"), config)
    all_hits = (1 << 40) - 1
    dipped = all_hits ^ (3 << 10)
    measured = replay.summary(dipped, 0)
    assert measured["missed_seconds"] == measured["missed_seconds_upper"] == 0
    assert replay.rank(dipped, 0) < replay.rank(all_hits, 1 << 20)
    assert replay.automatic_rank(dipped, 0) < replay.automatic_rank(all_hits, 1 << 20)


def test_on_delay_rejects_only_a_pulse_shorter_than_the_hold_plus_signal():
    positive = rows([i / 10 for i in range(100)])
    negative = rows([20 + i / 10 for i in range(100)], "not_present")
    all_hits = (1 << 100) - 1
    rejected = DurationReplay(positive, negative, {"timeout": 0.2, "on_delay": 1, "off_delay": 0})
    sustained = DurationReplay(positive, negative, {"timeout": 2, "on_delay": 1, "off_delay": 0})
    assert rejected.summary(all_hits, 1 << 50)["false_positive_seconds"] == 0
    assert sustained.summary(all_hits, 1 << 50)["false_positive_seconds"] > 0
    assert rejected.rank(all_hits, 1 << 50) == rejected.rank(all_hits, 0)


def test_avoiding_a_tiny_uncovered_miss_does_not_make_the_device_always_on():
    present = rows(range(5000), energy=70)
    present[-1][1]["g0_still"] = 14  # At a boundary; the outlier filter must retain it.
    absent = rows(range(6000, 11000), "not_present", energy=15)
    result = harness.fit(present + absent, ["g0_still"], timing={"timeout": 1})
    assert result["outlier_filter"]["human"]["excluded"]["present"] == 0
    assert result["proposals"]["g0_still"]["threshold"] >= 15
    assert result["training"]["duration"]["false_positive_seconds"] == 0


@pytest.mark.parametrize(
    "timing", [None, {"timeout": 1}, {"timeout": 1, "on_delay": 0.5, "off_delay": 1}]
)
def test_all_fit_reports_share_the_current_time_cost(timing):
    human = harness.row_samples({"g0_still": 30}, {"g0_still": 5})
    automatic = rows(range(1000, 1100), energy=30, confidence=0.8)
    automatic += rows(range(1200, 1300), "not_present", energy=5, confidence=0.7)
    result = harness.fit(
        human, ["g0_still"], current={"g0_still": 10}, automatic=automatic, timing=timing
    )
    assert result["targets"]["scoring"] == "weighted_post_timing_error"
    for name in (
        "training",
        "recent_training",
        "validation",
        "estimated_training",
        "current_validation",
    ):
        d = result[name]["duration"]
        expected = (
            500 * d["missed_seconds_upper"] / d["present_seconds"]
            + 100 * d["false_positive_seconds"] / d["empty_seconds"]
        )
        assert d["error_cost"] == pytest.approx(expected)
        assert d["missed_time_cost"] == 5

"""Room summaries aggregate simultaneous signals before downsampling."""

import pytest
from test_event_charts import row, view
from tuner_under_test.worker import display


def summary(events, *, blocks=None, keys=None):
    source = view(events, end=120, blocks=blocks)
    return display.unpack_chart(
        display.calculate(
            "charts",
            source,
            {
                "device_id": "a",
                "keys": keys or ["g0_move", "g0_still"],
                "hours": 0.1,
                "max_points": 50,
                "end": 120,
                "summary": True,
            },
        )
    )


def test_pulse_keeps_physical_peak_and_duration_weighted_strongest():
    result = summary([(0, row(0, 20)), (10, row(100, 20)), (11, row(0, 20))])
    peak = next(p for p in result["points"] if p["max"] == 100)
    assert (peak["max_start"], peak["max_end"]) == (10, 11)
    assert peak["avg"] == pytest.approx(20 + 80 / (peak["end"] - peak["start"]))
    assert sum(p["avg"] * p["observed_seconds"] for p in result["points"]) == pytest.approx(2480)


def test_boundary_preserves_both_endpoint_values_instead_of_max_deduplication():
    result = summary([(0, row(100, 20)), (48, row(0, 20))])
    at_boundary = [p for p in result["points"] if p["t"] == 48]
    assert [p["avg"] for p in at_boundary] == [100, 20]
    assert at_boundary[0]["anchor"] is True
    assert not at_boundary[1].get("anchor", False)


def test_changing_winner_integrates_simultaneous_maximum():
    result = summary([(0, row(80, 20)), (30, row(10, 70)), (60, row(40, 30))])
    integral = sum(p["avg"] * p["observed_seconds"] for p in result["points"])
    assert integral == pytest.approx(80 * 30 + 70 * 30 + 40 * 60)


def test_one_gate_missing_does_not_break_available_other_gate():
    result = summary([(0, row(60, 10)), (30, row(None, 10)), (90, row(60, 10))])
    assert not any(p["break_before"] for p in result["points"])
    assert sum(p["observed_seconds"] for p in result["points"]) == 120
    assert sum(p["avg"] * p["observed_seconds"] for p in result["points"]) == pytest.approx(4200)


def test_complete_outage_retains_true_gap_and_resume_break():
    result = summary([], blocks=[([(0, row(60, 10))], 30), ([(90, row(40, 10))], 120)])
    assert all(p["end"] <= 30 or p["start"] >= 90 for p in result["points"])
    assert next(p for p in result["points"] if p["start"] == 90)["break_before"]


def test_selected_kind_does_not_include_unrequested_gate():
    result = summary([(0, row(100, 20))], keys=["g0_still"])
    assert all(p["avg"] == 20 for p in result["points"])


@pytest.mark.parametrize("budget", [0, 50, 400, 1000, 100000])
def test_summary_keeps_same_window_and_point_budget_limits_as_native(budget):
    source = view([(0, row(100, 20)), (30, row(None, None)), (90, row(40, 10))], end=120)
    result = display.unpack_chart(
        display.calculate(
            "charts",
            source,
            {
                "device_id": "a",
                "keys": ["g0_move", "g0_still"],
                "hours": 0.1,
                "max_points": budget,
                "end": 120,
                "summary": True,
            },
        )
    )
    assert (result["start"], result["end"]) == (-240, 120)
    assert len(result["points"]) <= max(50, min(1000, budget))
    assert all(
        result["start"] <= p["start"] <= p["end"] <= result["end"]
        and p["min"] <= p["avg"] <= p["max"]
        for p in result["points"]
    )
    assert sum(p["observed_seconds"] for p in result["points"]) == 60

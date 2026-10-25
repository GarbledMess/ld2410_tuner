"""Search reuse retains the original floating-point operations and full rates."""

import random
from unittest.mock import patch

import pytest
from test_learning_efficiency import TIMINGS, wide_member
from tuner_under_test.calibration.intervals import WeightedTime, across
from tuner_under_test.rooms import replay, window_rates
from tuner_under_test.rooms.objective import Objective


def reference_integrals(self, index, hits):
    """The original per-window computation, without its memoization."""
    if not hits:
        return window_rates.ZERO
    (low, high), (false_low, false_high) = self.projector._window(index, hits)
    present = across(self.present, low, high) if low else (0.0, 0.0)
    empty = across(self.empty, false_low, false_high) if false_low else (0.0, 0.0)
    return tuple(round(value * window_rates.SCALE) for value in (*present, *empty))


@pytest.mark.parametrize("timing", TIMINGS)
def test_window_rates_are_bit_exact_to_original_integrals(timing):
    rng = random.Random(804)
    member = wide_member(rng, "a", timing, count=300, guessing=True, gap=600)
    item = Objective({"a": member}, 0, 9000)
    reference = Objective({"a": member}, 0, 9000)
    for _ in range(50):
        bits, automatic = rng.getrandbits(300), rng.getrandbits(300)
        with patch.object(window_rates.WindowRates, "_integrals", reference_integrals):
            expected = reference._rates([(bits, automatic)])
        assert repr(item._rates([(bits, automatic)])) == repr(expected)
    # Replacing masses must invalidate any knowledge of which classes contribute.
    times = item.mass["human", "present"].times
    for objective in (item, reference):
        objective.mass = {key: WeightedTime(times, [0.0] * len(times)) for key in item.mass}
        objective.totals = dict.fromkeys(objective.mass, 0.0)
    with patch.object(window_rates.WindowRates, "_integrals", reference_integrals):
        expected = reference._rates([(bits, automatic)])
    assert item._rates([(bits, automatic)]) == expected


@pytest.mark.parametrize("state, kind", [("present", "lower"), ("not_present", "upper")])
def test_unscored_class_does_not_project_spans(state, kind):
    member = wide_member(random.Random(806), "a", TIMINGS[3], count=300, gap=600)
    member["view"].data["devices"]["a"]["history_labels"] = [
        {"start": 0, "end": 9000, "state": state}
    ]
    item = Objective({"a": member}, 0, 9000)
    other = "not_present" if state == "present" else "present"
    assert item.totals["human", other] == 0
    with patch.object(replay, "window_spans", wraps=replay.window_spans) as spans:
        item._rates([(123456789, 123456789)])
    assert spans.call_count
    assert all(call.args[2] == kind for call in spans.call_args_list)

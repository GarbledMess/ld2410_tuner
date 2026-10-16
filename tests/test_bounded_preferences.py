"""Represented-time preferences match independent unit-duration evidence."""

import copy
import random
from collections import Counter
from types import SimpleNamespace

import test_tuner  # noqa: F401
from tuner_under_test.calibration import event_preferences


def oracle(cells, current, weights, supports, reference=()):
    hist = {
        key: {
            (source, state): Counter()
            for source in ("human", "automatic")
            for state in ("present", "not_present")
        }
        for key in current
    }
    for start, end, values, state, source, _confidence in cells:
        for key in current:
            if key not in values:
                continue
            value = values[key]
            if source == "automatic" and state == "present" and not supports(key, value):
                continue
            # Independent integer repetition oracle, without weighted histogram arithmetic.
            for _ in range(int((end - start) * weights[start])):
                hist[key][source, state][value] += 1
    results = {}
    for key, fallback in current.items():
        human = {state: hist[key]["human", state] for state in ("present", "not_present")}
        selected = {
            state: list(
                (
                    values if values or state in reference else hist[key]["automatic", state]
                ).elements()
            )
            for state, values in human.items()
        }
        noise, presence = sorted(selected["not_present"]), sorted(selected["present"])
        ceiling = max(noise) if noise else None
        occupied = presence[len(presence) // 4] if presence else None
        separated = ceiling is not None and occupied is not None and occupied > ceiling
        supported = separated and all(human.values())
        preferred = fallback
        if supported:
            preferred = (ceiling + occupied) // 2
        elif noise:
            preferred = min(100, noise[int(len(noise) * 0.99)] + 2)
        results[key] = {
            "preferred_threshold": preferred,
            "noise_ceiling": ceiling,
            "presence_reference": occupied,
            "separated": separated,
            "human_supported": bool(supported),
        }
    return results


def test_seeded_scaled_preferences_match_unit_repetition_and_ignore_confidence():
    rng = random.Random(61931)
    current = {"a": 50, "b": 37, "automatic-only": 44}
    cells, weights = [], {}
    for i in range(160):
        source = "human" if i % 3 else "automatic"
        values = {
            key: rng.randrange(101)
            for key in current
            if rng.randrange(5) and (key != "automatic-only" or source == "automatic")
        }
        cells.append(
            (i * 2, i * 2 + 2, values, "present" if i % 2 else "not_present", source, rng.random())
        )
        weights[i * 2] = rng.randrange(5)

    def supports(key, value):
        return value >= 35

    model = SimpleNamespace(supports=supports)
    before = copy.deepcopy(cells)
    for reference in ((), ("present", "not_present")):
        expected = oracle(cells, current, weights, supports, reference)
        assert (
            event_preferences.preferences(
                cells, current, quality=model, reference=reference, scale=weights.__getitem__
            )
            == expected
        )
    assert cells == before


def test_zero_mass_halo_cannot_raise_noise_ceiling_or_change_weighted_quantiles():
    cells = [
        (0, 1, {"g": 10}, "not_present", "human", 0.01),
        (1, 2, {"g": 90}, "not_present", "human", 1),
        (2, 3, {"g": 30}, "present", "human", 1),
        (3, 4, {"g": 80}, "present", "human", 1),
        (4, 5, {"g": 100}, "not_present", "human", 1),
    ]
    weights = {0: 100, 1: 1, 2: 1, 3: 10, 4: 0}
    found = event_preferences.preferences(cells, {"g": 50}, scale=weights.__getitem__)["g"]
    assert found["noise_ceiling"] == 90
    assert found["presence_reference"] == 80
    assert found["preferred_threshold"] == 12


def test_prepared_masses_are_fixed_and_do_not_leak_to_other_calls():
    cells = [
        (0, 2, {"g": 10}, "not_present", "human", 1),
        (2, 4, {"g": 30}, "present", "human", 0.01),
    ]
    weights = {0: 3, 2: 4}
    prepared = event_preferences.prepare(cells, ["g"], scale=weights.__getitem__)
    frozen = copy.deepcopy(prepared)
    expected = event_preferences.preferences(cells, {"g": 50}, scale=weights.__getitem__)
    cells[0][2]["g"] = 99
    weights[0] = 0
    assert (
        event_preferences.preferences(
            cells, {"g": 50}, prepared=prepared, scale=lambda timestamp: 1000
        )
        == expected
    )
    assert prepared == frozen
    assert event_preferences.prepare(cells, ["g"], scale=weights.__getitem__) != prepared


def test_default_histogram_path_preserves_original_elapsed_mass_exactly():
    cells = [
        (0.1, 0.3, {"g": 12}, "not_present", "human", 0.1),
        (0.3, 0.7, {"g": 12}, "not_present", "human", 0.9),
        (0.7, 1.1, {"g": 40}, "present", "automatic", 0.2),
    ]
    prepared = event_preferences.prepare(cells, ["g"])
    assert prepared["g"]["human", "not_present"][12] == (0.3 - 0.1) + (0.7 - 0.3)
    assert prepared["g"]["automatic", "present"][40] == 1.1 - 0.7
    assert prepared == event_preferences.prepare(cells, ["g"], scale=None)

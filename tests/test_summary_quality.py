"""Synthetic summary evidence preserves independent physical quality votes."""

import random
from dataclasses import fields
from math import inf
from unittest.mock import patch

import pytest
import test_tuner  # noqa: F401
from test_event_quality import view
from tuner_under_test.calibration import event_quality, summary_quality
from tuner_under_test.const import HISTORY_KEYS
from tuner_under_test.history import guesses, spans
from tuner_under_test.history.label_lookup import _history_label_reader


def assert_equivalent(expected, actual):
    for name in ("episodes", "raw_runs", "start", "end", "gate_presence"):
        assert getattr(actual, name) == getattr(expected, name), name
    for name in ("automatic_episodes", "overlaps"):
        left, right = getattr(expected, name), getattr(actual, name)
        assert len(left) == len(right)
        for old, new in zip(left, right, strict=True):
            for field in fields(old):
                a, b = getattr(old, field.name), getattr(new, field.name)
                assert b == pytest.approx(a, abs=1e-3) if isinstance(a, float) else b == a
    assert actual.gate_empty.keys() == expected.gate_empty.keys()
    for key in expected.gate_empty:
        assert actual.gate_empty[key] == pytest.approx(expected.gate_empty[key], abs=1e-3)
    for name in ("gate_occupied_histograms", "gate_empty_histograms"):
        for key, old in getattr(expected, name).items():
            new = getattr(actual, name)[key]
            assert len(new) == len(old)
            for a, b in zip(old, new, strict=True):
                assert b == pytest.approx(a, abs=1e-3)


@pytest.mark.parametrize("seed", [5, 71, 839])
def test_seeded_gaps_missing_labels_runs_and_ownership_match_physical_collection(seed):
    rng = random.Random(seed)
    stretches = []
    for low, high in ((0, 50), (70, 110), (130, 180)):
        events = [
            (i + 0.125, tuple(rng.choice((None, 0, 10.1, 30, 99.9)) for _ in HISTORY_KEYS))
            for i in range(low, high)
        ]
        stretches.append((events, high))
    labels = [
        {"start": 0, "end": 30, "state": "present"},
        {"start": 30, "end": 90, "state": "not_present"},
        {"start": 100, "end": 115, "state": "unknown"},
    ]
    runs = [
        (0, 45, "present", 20, guesses.EXTERNAL),
        (45, 120, "not_present", 14, guesses.EXTERNAL),
        (120, 180, "present", 1, guesses.EXTERNAL),
    ]
    source = view(stretches, labels, runs)
    independent = _history_label_reader(
        {"history_labels": [{"start": 15, "end": 170, "state": "present"}]}
    )
    for keys in ([HISTORY_KEYS[0]], [HISTORY_KEYS[17], HISTORY_KEYS[3]], []):
        args = (source, "any-name", keys, 2.375, 177.625, independent, ("room", "other-radar"))
        expected = event_quality.collect(*args)
        with patch.object(spans, "events", side_effect=AssertionError("physical vector replay")):
            actual = summary_quality.collect(*args)
        assert_equivalent(expected, actual)
        assert actual.fingerprint == summary_quality.collect(*args).fingerprint


def test_only_selected_gate_coverage_counts_source_votes_and_preserves_parent_across_outage():
    values = [(0, (20,) + (None,) * 17), (10, (None, 20) + (None,) * 16)]
    source = view(
        [(values, 20), ([(30, (20,) + (None,) * 17)], 40)],
        [{"start": 0, "end": 40, "state": "present"}],
        [(0, 40, "present", 20, guesses.EXTERNAL)],
    )
    args = (source, "any-name", [HISTORY_KEYS[0]], -inf, inf, None, None)
    actual = summary_quality.collect(*args)
    assert_equivalent(event_quality.collect(*args), actual)
    assert len(actual.episodes) == 1
    assert actual.overlaps[0].observed == 20


def test_empty_window_and_hidden_future_match_and_future_changes_do_not_affect_fingerprint():
    source = view(
        [([(0, (20,) * 18), (10, (99,) * 18)], 20)],
        [{"start": 0, "end": 20, "state": "not_present"}],
    )
    source._attribution_until = 10
    args = (source, "any-name", [HISTORY_KEYS[0]], 0, inf, None, None)
    assert_equivalent(event_quality.collect(*args), summary_quality.collect(*args))
    empty = (source, "any-name", [HISTORY_KEYS[0]], 5, 5, None, None)
    assert_equivalent(event_quality.collect(*empty), summary_quality.collect(*empty))


def test_cross_day_episode_lineage_and_automatic_basis_changes_match(tmp_path):
    from array import array
    from types import SimpleNamespace

    from tuner_under_test.history import dayfile, daystore

    store = daystore.DayStore(tmp_path / "synthetic")
    for low, high, times, ends, values, runs in (
        (0, 86400, [86390], [86400], [30], [(86390, 86400, "present", 20, guesses.EXTERNAL)]),
        (
            86400,
            172800,
            [86400, 86405, 86420],
            [86405, 86410, 86430],
            [30, 10, 40],
            [
                (86400, 86405, "present", 20, guesses.EXTERNAL),
                (86405, 86430, "present", 18, guesses.UNKNOWN),
            ],
        ),
    ):
        day = dayfile.Day(
            low, high, array("d", times), array("d", ends), [bytes(values)] * 18, runs
        )
        store.replace("any-name", daystore.date_of(low), day)
    source = SimpleNamespace(
        _days=store,
        data={
            "devices": {
                "any-name": {"history_labels": [{"start": 86380, "end": 86440, "state": "present"}]}
            }
        },
    )
    args = (source, "any-name", list(HISTORY_KEYS), 86395, 86425, None, ("owner",))
    assert_equivalent(event_quality.collect(*args), summary_quality.collect(*args))


def test_hidden_future_energy_labels_and_other_device_do_not_change_earlier_evidence():
    contexts = []
    for future in (1, 99):
        source = view(
            [([(0, (20,) * 18), (10, (future,) * 18)], 20)],
            [
                {"start": 0, "end": 10, "state": "not_present"},
                {"start": 10, "end": 20, "state": "present" if future == 99 else "unknown"},
            ],
        )
        source._attribution_until = 10
        source.data["devices"]["unrelated"] = {
            "history_labels": [
                {"start": 0, "end": 10, "state": "present" if future == 99 else "not_present"}
            ]
        }
        args = (source, "any-name", [HISTORY_KEYS[0]], 0, inf, None, None)
        expected = event_quality.collect(*args)
        with patch.object(
            event_quality, "_cells", side_effect=AssertionError("materialized cells")
        ):
            actual = summary_quality.collect(*args)
        assert_equivalent(expected, actual)
        contexts.append(actual)
    assert contexts[0] == contexts[1]


@pytest.mark.parametrize("start,end", [(5, 4), (float("nan"), 10), (0, float("nan"))])
def test_invalid_physical_windows_match_existing_rejection(start, end):
    source = view([([(0, (20,) * 18)], 20)], [])
    with pytest.raises(ValueError, match="physical history window"):
        summary_quality.collect(source, "any-name", [HISTORY_KEYS[0]], start, end, None, None)


def test_selected_gate_coverage_is_scanned_once_per_day_not_per_annotation():
    from tuner_under_test.history import summaries

    source = view(
        [([(i, (20,) + (None,) * 17) for i in range(40)], 40)],
        [
            {"start": i, "end": i + 1, "state": "present" if i % 2 else "not_present"}
            for i in range(40)
        ],
    )
    calls = []
    valid_ranges = summaries.Summary.valid_ranges

    def counted(summary, low, high, gates):
        calls.append((low, high, tuple(gates)))
        return valid_ranges(summary, low, high, gates)

    args = (source, "any-name", [HISTORY_KEYS[0]], 0, 40, None, None)
    expected = event_quality.collect(*args)
    with patch.object(summaries.Summary, "valid_ranges", counted):
        actual = summary_quality.collect(*args)
    assert_equivalent(expected, actual)
    assert calls == [(0, 40, (0,))]


def test_physical_retention_clipping_and_query_after_recording_match_without_mutating_day():
    source = view(
        [([(0, (20,) * 18), (10, (40,) * 18)], 20)], [{"start": 0, "end": 20, "state": "present"}]
    )
    source._physical_history_start = 7
    date = source._days.days("any-name")[0]
    day = source._days.day("any-name", date, until=20)
    before = day.times.tobytes(), day.ends.tobytes(), tuple(bytes(c) for c in day.columns)
    for low, high in ((2.5, 17.5), (30, 40)):
        args = (source, "any-name", [HISTORY_KEYS[0]], low, high, None, None)
        assert_equivalent(event_quality.collect(*args), summary_quality.collect(*args))
    assert (day.times.tobytes(), day.ends.tobytes(), tuple(bytes(c) for c in day.columns)) == before

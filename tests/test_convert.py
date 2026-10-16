"""One-way conversion of history kept in the JSON store into day files."""

import datetime

import pytest
import test_tuner  # noqa: F401
from legacy_formats import guess_chunks, sampled_block, samples
from tuner_under_test.history import convert, daystore, guesses

DAY = datetime.date(2025, 10, 15)
START = 1_760_486_400.0
NOW = START + 2 * 86_400


def store(tmp_path):
    days = daystore.DayStore(tmp_path / "history")
    days.open(now=NOW)
    return days


def first_gate(days, device_id="radar", date=DAY):
    day = days.day(device_id, date)
    return [
        (t, e, day.vector(i)[0]) for i, (t, e) in enumerate(zip(day.times, day.ends, strict=True))
    ]


def converted(tmp_path, device):
    days = store(tmp_path)
    convert.run({"devices": {"radar": device}}, days, NOW)
    return days


def test_sampled_rows_hold_until_the_next_row_and_gaps_end_coverage(tmp_path):
    history = [samples(START, [10, 11, 12]), samples(START + 100, [20])]
    assert first_gate(converted(tmp_path, {"history": history})) == [
        (START, START + 6, 10),
        (START + 6, START + 12, 11),
        (START + 12, START + 15, 12),  # Gap > GAP: ends half a sample after the last row.
        (START + 100, START + 103, 20),
    ]


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_every_stored_block_version_converts_its_energies(tmp_path, version):
    rows = [(0, [10] + [255] * 17), (6, [12] + [3] * 17)]
    if version in (2, 3):
        rows = [(*row, 0, 0) for row in rows]  # No guess in these rows.
    days = converted(tmp_path, {"history": [sampled_block(START, rows, version)]})
    day = days.day("radar", DAY)
    assert first_gate(days) == [(START, START + 6, 10), (START + 6, START + 9, 12)]
    assert day.vector(0)[1] is None
    assert day.vector(1)[1] == 3


def test_old_layout_row_guesses_become_runs(tmp_path):
    rows = [
        (0, [8] * 18, 2, 83),  # Not present at 83 %: 17 steps.
        (6, [8] * 18, 2, 84),  # The same guess (17 steps): no new run.
        (12, [9] * 18, 1, 99),  # Present at 99 %: 20 steps.
        (18, [9] * 18, 0, 0),  # No guess: the run ends.
    ]
    days = converted(tmp_path, {"history": [sampled_block(START, rows, 3)]})
    assert days.day("radar", DAY).guesses == [
        (START, START + 12, "not_present", 17, guesses.UNKNOWN),
        (START + 12, START + 18, "present", 20, guesses.UNKNOWN),
    ]


def test_a_later_duplicate_row_replaces_the_earlier_row_and_its_guess(tmp_path):
    guessed = sampled_block(START, [(0, [8] * 18, 1, 90)], 2)
    plain = sampled_block(START, [(0, [9] * 18, 0, 0)], 2)
    days = converted(tmp_path, {"history": [guessed, plain]})
    assert first_gate(days)[0][2] == 9
    assert days.day("radar", DAY).guesses == []
    days = converted(tmp_path / "again", {"history": [plain, guessed]})
    assert first_gate(days)[0][2] == 8
    assert [run[2] for run in days.day("radar", DAY).guesses] == ["present"]


def test_guess_runs_convert_with_their_levels_and_bases(tmp_path):
    runs = [
        (START, START + 6, "present", 18, guesses.REFERENCE),
        (START + 6, START + 12, "not_present", 19, guesses.EXTERNAL),
    ]
    device = {"history": [samples(START, [10, 10, 10])], "guess_runs": guess_chunks(runs)}
    assert converted(tmp_path, device).day("radar", DAY).guesses == runs


def test_conversion_is_idempotent_and_removes_history_from_the_json_data(tmp_path):
    device = {"history": [samples(START, [10, 11, 12])], "guess_runs": [], "history_labels": []}
    data = {"devices": {"radar": device, "empty": {"history_labels": []}}}
    first = store(tmp_path)
    assert convert.run({"devices": {"radar": dict(device)}}, first, NOW)  # Interrupted run.
    days = store(tmp_path)
    assert convert.run(data, days, NOW)
    assert first_gate(days) == first_gate(first)
    assert "history" not in data["devices"]["radar"]
    assert "guess_runs" not in data["devices"]["radar"]
    assert data["devices"]["radar"]["history_labels"] == []
    assert not convert.run(data, days, NOW)  # Nothing left to convert.
    assert days.days("empty") == []


def test_large_histories_flush_in_batches(tmp_path, monkeypatch):
    monkeypatch.setattr(convert, "BATCH", 2)
    days = converted(tmp_path, {"history": [samples(START, [10, 11, 12, 13, 14])]})
    assert [value for _t, _e, value in first_gate(days)] == [10, 11, 12, 13, 14]


@pytest.mark.parametrize(
    "field,broken",
    [
        ("history", {"data": "corrupt"}),
        ("history", {"version": 9, "start": START, "count": 1, "data": "!"}),
        # Never written by a release (an unreleased format): unreadable, not imported.
        ("history", {"kind": "transitions", "start": START, "end": START + 1, "data": "!"}),
        ("open_event_history", {"kind": "transitions", "data": "!"}),
        ("guess_runs", [{"version": 1, "start": START, "end": START + 1, "count": 1, "data": "!"}]),
    ],
)
def test_skipping_invalid_history_invalidates_its_learned_recommendation(tmp_path, field, broken):
    device = {
        "history": [samples(START, [10, 20])],
        "last_learning": {"status": "ok"},
        "label_revision": 4,
    }
    if field == "history":
        device[field].append(broken)
    else:
        device[field] = broken
    days = converted(tmp_path, device)
    assert "last_learning" not in device
    assert device["label_revision"] == 5
    assert device["history_cleanup"]["invalid_blocks"] == 1
    assert [value for _t, _e, value in first_gate(days)] == [10, 20]


def test_untimed_legacy_summaries_are_dropped_with_what_was_learned_from_them(tmp_path):
    device = {
        "history_legacy_histograms": {"g0_move": {"present": [0] * 20 + [10]}},
        "history_legacy_since": START,
        "samples": {"g0_move": {"present": [30, 31]}},  # The oldest layout: raw value arrays.
        "last_learning": {"status": "ok"},
        "label_revision": 2,
    }
    days = store(tmp_path)
    assert convert.run({"devices": {"radar": device}}, days, NOW)
    assert "history_legacy_histograms" not in device
    assert "history_legacy_since" not in device
    assert "samples" not in device
    assert "last_learning" not in device
    assert device["label_revision"] == 3
    assert days.days("radar") == []

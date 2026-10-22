"""Ingestion: delivered changes and guess-run changes into the day store; coverage ends only
on explicit events."""

import datetime
from types import SimpleNamespace

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.history import daystore, guesses, ingest

DAY = datetime.date(2025, 10, 15)
START = 1_760_486_400.0


def runtime(tmp_path, **live):
    days = daystore.DayStore(tmp_path / "history")
    days.open(now=START)
    return SimpleNamespace(
        data={"devices": {"radar": {"recording_enabled": True}}},
        _days=days,
        _live={"radar": dict(live)},
        _storage_status={},
    )


def rows(live, until):
    day = live._days.day("radar", DAY, until=until)
    return [
        (time, end, day.vector(index)[:2])
        for index, (time, end) in enumerate(zip(day.times, day.ends, strict=True))
    ]


def test_session_starts_with_the_full_current_vector(tmp_path):
    live = runtime(tmp_path, g0_move=10, g0_still=20)
    assert ingest.change(live, "radar", {"g0_move": 11}, START + 1)
    assert rows(live, START + 2) == [(START + 1, START + 2, (11, 20))]


def test_fractional_energy_changes_within_one_percentage_bin_do_not_add_rows(tmp_path):
    live = runtime(tmp_path, g0_move=40.01, g0_still=99.9)
    ingest.change(live, "radar", {}, START + 1)
    ingest.change(live, "radar", {"g0_move": 40.9}, START + 2)
    ingest.change(live, "radar", {"g0_move": 41}, START + 3)
    assert rows(live, START + 4) == [(START + 1, START + 4, (41, 100))]
    day = live._days.day("radar", DAY, until=START + 4)
    assert all(isinstance(column, bytes) for column in day.columns)


def test_pulse_keeps_event_times_and_unchanged_values_store_nothing(tmp_path):
    live = runtime(tmp_path, g0_move=0)
    for at, value in [(1.125, 0), (1.175, 40), (1.225, 0), (1.5, 0)]:
        ingest.change(live, "radar", {"g0_move": value}, START + at)
    assert rows(live, START + 2) == [
        (START + 1.125, START + 1.175, (0, None)),
        (START + 1.175, START + 1.225, (40, None)),
        (START + 1.225, START + 2, (0, None)),
    ]


def test_unavailable_marks_one_gate_missing_without_ending_the_others(tmp_path):
    live = runtime(tmp_path, g0_move=10, g0_still=20)
    ingest.change(live, "radar", {}, START + 1)
    ingest.change(live, "radar", {"g0_still": None}, START + 2)
    assert rows(live, START + 3)[-1] == (START + 2, START + 3, (10, None))


def test_pause_ends_coverage_and_resume_starts_a_full_vector(tmp_path):
    live = runtime(tmp_path, g0_move=10)
    ingest.change(live, "radar", {}, START + 1)
    live.data["devices"]["radar"]["recording_enabled"] = False
    assert not ingest.change(live, "radar", {"g0_move": 12}, START + 5)
    live.data["devices"]["radar"]["recording_enabled"] = True
    live._live["radar"]["g0_move"] = 13
    ingest.change(live, "radar", {}, START + 9)
    assert rows(live, START + 10) == [
        (START + 1, START + 5, (10, None)),
        (START + 9, START + 10, (13, None)),
    ]


def test_blocked_storage_recovery_and_removal_end_coverage(tmp_path):
    for block in (
        lambda live: live._storage_status.update(blocked=True),
        lambda live: live.data["devices"]["radar"].update(
            configuration_recovery={"status": "running"}
        ),
        lambda live: live.data["devices"].pop("radar"),
    ):
        live = runtime(tmp_path / str(id(block)), g0_move=10)
        ingest.change(live, "radar", {}, START + 1)
        block(live)
        assert not ingest.change(live, "radar", {"g0_move": 11}, START + 4)
        assert live._days.current("radar") is None
        assert live._days.day("radar", DAY, until=START + 9).ends[-1] == START + 4


def test_clock_step_back_is_recorded_at_the_last_recorded_time(tmp_path):
    live = runtime(tmp_path, g0_move=10)
    ingest.change(live, "radar", {}, START + 10)
    ingest.change(live, "radar", {"g0_move": 11}, START + 7)
    # Recorded at START + 10, the same millisecond as the first reading, so it replaces it.
    assert rows(live, START + 11) == [(START + 10, START + 11, (11, None))]


def test_guess_runs_keep_the_existing_hysteresis(tmp_path):
    live = runtime(tmp_path, g0_move=10)
    ingest.change(live, "radar", {}, START)
    for at, sample in [
        (1, guesses.guess("present", 90, guesses.REFERENCE)),
        (2, guesses.guess("present", 92, guesses.REFERENCE)),
        (3, guesses.guess("present", 70, guesses.REFERENCE)),
        (4, None),
    ]:
        ingest.guess(live, "radar", START + at, sample)
    day = live._days.day("radar", DAY, until=START + 9)
    assert [(run[0], run[1], run[3]) for run in day.guesses] == [
        (START + 1, START + 3, guesses.level(90)),
        (START + 3, START + 4, guesses.level(70)),
    ]


def test_stop_ends_coverage_for_every_open_radar(tmp_path):
    live = runtime(tmp_path, g0_move=10)
    live.data["devices"]["other"] = {}
    live._live["other"] = {"g0_move": 3}
    ingest.change(live, "radar", {}, START + 1)
    ingest.change(live, "other", {}, START + 1)
    ingest.stop(live, START + 6)
    assert live._days.current("radar") is None and live._days.current("other") is None
    assert live._days.day("other", DAY, until=START + 9).ends[-1] == START + 6


def test_no_store_unknown_device_or_no_readings_record_nothing(tmp_path):
    live = runtime(tmp_path)
    assert not ingest.change(live, "radar", {}, START + 1)  # Nothing observed yet.
    assert not ingest.change(live, "missing", {"g0_move": 1}, START + 1)
    live._days = None
    assert not ingest.change(live, "radar", {"g0_move": 1}, START + 1)
    ingest.stop(live, START + 2)


def test_guesses_need_open_coverage_and_no_guess_without_a_run_records_nothing(tmp_path):
    live = runtime(tmp_path, g0_move=10)
    ingest.guess(live, "radar", START + 1, guesses.guess("present", 90, guesses.REFERENCE))
    assert live._days.run("radar") is None
    ingest.change(live, "radar", {}, START + 2)
    ingest.guess(live, "radar", START + 3, None)
    assert live._days.day("radar", DAY, until=START + 4).guesses == []


def test_unchanged_readings_checkpoint_at_flush_without_adding_vectors(tmp_path, monkeypatch):
    live = runtime(tmp_path, g0_move=10)
    ingest.change(live, "radar", {}, START + 1)
    written = live._days.flush(now=START + 2)
    ingest.change(live, "radar", {"g0_move": 10}, START + 50)  # Polled, nothing changed.
    assert not live._days._live["radar"].buffer  # Polling itself adds no records.
    assert live._days.flush(now=START + 51) > 0  # Flush journals the observation time.
    assert live._days.flush(now=START + 52) == 0  # No duplicate checkpoint.
    monkeypatch.setattr(daystore.time, "time", lambda: START + 10)  # A clock behind the poll.
    assert rows(live, None) == [(START + 1, START + 50, (10, None))]
    assert written > 0


def test_present_evidence_vetoes_only_quiet_observed_duration(tmp_path):
    from tuner_under_test.history import dayfile

    live = runtime(tmp_path)
    live._history_until = START + 30
    live._history_runtime = {}
    baseline = [0] * 101
    baseline[5] = 100
    live.data["devices"]["radar"]["histograms"] = {"g0_move": {"not_present": baseline}}
    for record in [
        dayfile.Change(START, {0: 5}),
        dayfile.Guess(START, "present", 20, guesses.REFERENCE),
        dayfile.Change(START + 7.5, {0: 60}),
        dayfile.Change(START + 18.25, {0: 5}),
        dayfile.End(START + 30),
    ]:
        live._days.append("radar", record)
    # Native evidence must never ask the retired sampled reader for quiet vetoes.
    live._iter_history_samples = lambda *args, **kwargs: iter(())
    result = guesses.evidence(live, "radar").runs
    assert [(run[0], run[1]) for run in result] == [(START + 7.5, START + 18.25)]


@pytest.mark.parametrize("previous_finished", [False, True])
def test_buffer_confirmation_preserves_both_sides_of_midnight_after_restart(
    tmp_path, monkeypatch, previous_finished
):
    live = runtime(tmp_path, g0_move=10)
    midnight = START + 86400
    now = midnight + 20
    monkeypatch.setattr(ingest.time, "time", lambda: now)
    ingest.change(live, "radar", {}, midnight - 20)
    ingest.change(live, "radar", {"g0_move": 10}, midnight + 1)
    if previous_finished:
        live._days.flush(now=midnight + 2)
    live._history_until = now
    assert ingest.confirm(live, "radar", midnight - 10, midnight + 10, 90)
    expected = [(midnight - 10, midnight + 10, "present", guesses.level(90), guesses.EXTERNAL)]
    assert guesses.of(live, "radar").runs == expected
    ingest.end(live, "radar", now + 5)
    live._days.flush(now=now + 6)
    assert guesses.of(live, "radar").runs == expected
    reopened = daystore.DayStore(live._days.root)
    reopened.open(now=now + 7)
    live._days = reopened
    assert guesses.of(live, "radar").runs == expected


def test_rollover_confirmation_fills_gaps_without_overwriting_existing_guesses(
    tmp_path, monkeypatch
):
    live = runtime(tmp_path, g0_move=10)
    midnight = START + 86400
    now = midnight + 20
    monkeypatch.setattr(ingest.time, "time", lambda: now)
    ingest.change(live, "radar", {}, midnight - 20)
    ingest.guess(live, "radar", midnight - 8, guesses.guess("not_present", 80, guesses.REFERENCE))
    ingest.guess(live, "radar", midnight - 3, None)
    ingest.change(live, "radar", {"g0_move": 10}, midnight + 1)
    ingest.guess(live, "radar", midnight + 2, guesses.guess("present", 80, guesses.REFERENCE))
    ingest.guess(live, "radar", midnight + 4, None)
    live._history_until = now
    assert ingest.confirm(live, "radar", midnight - 10, midnight + 10, 90)
    ext = ("present", guesses.level(90), guesses.EXTERNAL)
    expected = [
        (midnight - 10, midnight - 8, *ext),
        (midnight - 8, midnight - 3, "not_present", guesses.level(80), guesses.REFERENCE),
        (midnight - 3, midnight + 2, *ext),
        (midnight + 2, midnight + 4, "present", guesses.level(80), guesses.REFERENCE),
        (midnight + 4, midnight + 10, *ext),
    ]
    assert guesses.of(live, "radar").runs == expected
    ingest.end(live, "radar", now + 5)
    live._days.flush(now=now + 6)
    reopened = daystore.DayStore(live._days.root)
    reopened.open(now=now + 7)
    live._days = reopened
    assert guesses.of(live, "radar").runs == expected


def test_a_confirmation_about_a_finished_day_survives_a_crash_before_its_day_is_rewritten(
    tmp_path, monkeypatch
):
    live = runtime(tmp_path, g0_move=10)
    midnight = START + 86400
    now = midnight + 20
    monkeypatch.setattr(ingest.time, "time", lambda: now)
    ingest.change(live, "radar", {}, midnight - 20)
    ingest.change(live, "radar", {"g0_move": 10}, midnight + 1)
    live._days.flush(now=midnight + 2)
    assert ingest.confirm(live, "radar", midnight - 10, midnight + 10, 90)
    # The log is written, then the process stops before the finished day is rewritten.
    monkeypatch.setattr(daystore.DayStore, "_write_late", lambda *args: None)
    live._days.flush(now=now + 1)
    monkeypatch.undo()
    monkeypatch.setattr(ingest.time, "time", lambda: now)
    reopened = daystore.DayStore(live._days.root)
    reopened.open(now=now + 2)
    live._days = reopened
    expected = [(midnight - 10, midnight + 10, "present", guesses.level(90), guesses.EXTERNAL)]
    assert guesses.of(live, "radar").runs == expected
    reopened.flush(now=now + 3)
    again = daystore.DayStore(live._days.root)
    previous = again.day("radar", daystore.date_of(midnight - 1))
    assert previous.guesses == [(midnight - 10, midnight, *expected[0][2:])]


def test_a_confirmation_reaching_an_unrecorded_day_keeps_its_guess(tmp_path, monkeypatch):
    live = runtime(tmp_path, g0_move=10)
    midnight = START + 86400
    monkeypatch.setattr(ingest.time, "time", lambda: midnight + 20)
    ingest.change(live, "radar", {"g0_move": 10}, midnight + 1)
    assert ingest.confirm(live, "radar", midnight - 10, midnight + 10, 90)
    earlier = daystore.date_of(midnight - 1)
    assert earlier in live._days.days("radar")
    run = (midnight - 10, midnight, "present", guesses.level(90), guesses.EXTERNAL)
    assert live._days.day("radar", earlier).guesses == [run]
    live._days.flush(now=midnight + 21)
    assert daystore.DayStore(live._days.root).day("radar", earlier).guesses == [run]
    live._days.remove_device("radar")
    assert live._days.days("radar") == []


def test_a_confirmation_recorded_while_yesterday_is_being_finished_is_kept(tmp_path, monkeypatch):
    from tuner_under_test.history import dayfile

    live = runtime(tmp_path, g0_move=10)
    midnight = START + 86400
    now = midnight + 20
    monkeypatch.setattr(ingest.time, "time", lambda: now)
    ingest.change(live, "radar", {}, midnight - 100)
    ingest.change(live, "radar", {"g0_move": 10}, midnight + 1)  # Yesterday is closing.
    real = dayfile.encode_day
    done = []

    def finishing(day):
        if not done:  # The event loop records a confirmation mid-finish.
            done.append(ingest.confirm(live, "radar", midnight - 50, midnight + 5, 90))
        return real(day)

    monkeypatch.setattr(dayfile, "encode_day", finishing)
    live._days.flush(now=now + 1)
    monkeypatch.setattr(dayfile, "encode_day", real)
    live._days.flush(now=now + 2)
    assert done == [True]
    run = (midnight - 50, midnight, "present", guesses.level(90), guesses.EXTERNAL)
    reopened = daystore.DayStore(live._days.root)
    assert reopened.day("radar", daystore.date_of(midnight - 1)).guesses == [run]


def test_clear_during_a_flush_does_not_bring_a_day_back(tmp_path, monkeypatch):
    from tuner_under_test.history import dayfile

    live = runtime(tmp_path, g0_move=10)
    midnight = START + 86400
    now = midnight + 20
    monkeypatch.setattr(ingest.time, "time", lambda: now)
    ingest.change(live, "radar", {}, midnight - 100)
    ingest.change(live, "radar", {"g0_move": 10}, midnight + 1)
    live._days.flush(now=midnight + 2)
    assert ingest.confirm(live, "radar", midnight - 50, midnight + 5, 90)
    real = dayfile.splice

    def cleared(day, spans):
        live._days.remove_device("radar")  # Clear lands between taking and writing the span.
        return real(day, spans)

    monkeypatch.setattr(dayfile, "splice", cleared)
    live._days.flush(now=now + 1)
    monkeypatch.setattr(dayfile, "splice", real)
    assert live._days.days("radar") == []
    assert not list(live._days.folder("radar").glob("*.day"))

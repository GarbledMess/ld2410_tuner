"""Day files: append-log records for the live day, columnar zlib files for finished days."""

import math

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.history import dayfile

START = 1_760_486_400.0  # 2025-10-15T00:00:00Z
END = START + 86_400
MISSING = (None,) * 18


def vector(**gates):
    values = list(MISSING)
    for key, value in gates.items():
        values[int(key[1:])] = value
    return tuple(values)


def change(seconds, **gates):
    return dayfile.Change(START + seconds, {int(key[1:]): value for key, value in gates.items()})


def test_change_records_round_trip_with_millisecond_times():
    records = [change(1.125, g0=0), change(1.175, g0=40), change(1.225, g0=0)]
    data = dayfile.encode_records(records, START)
    assert dayfile.decode_log(data, START) == records


def test_truncated_tail_is_ignored():
    records = [change(1, g0=10, g3=20), change(2, g0=11)]
    data = dayfile.encode_records(records, START)
    assert dayfile.decode_log(data[:-1], START) == records[:1]


def test_same_millisecond_changes_merge_into_one_row():
    records = [change(100.0001, g0=5), change(100.0004, g1=7)]
    day = dayfile.day_from_log(records, None, START, END)
    assert list(day.times) == [START + 100]
    assert day.vector(0) == vector(g0=5, g1=7)


def test_end_record_closes_coverage_without_bridging():
    records = [change(10, g0=5), dayfile.End(START + 20), change(50, g0=5)]
    day = dayfile.day_from_log(records, None, START, END)
    assert list(day.times) == [START + 10, START + 50]
    assert list(day.ends) == [START + 20, END]


def test_non_integer_gate_uses_float_column_and_integer_gates_stay_bytes():
    records = [change(1, g0=40.125, g1=3), change(2, g0=41, g1=4)]
    day = dayfile.decode_day(dayfile.encode_day(dayfile.day_from_log(records, None, START, END)))
    assert day.vector(0)[:2] == (40.125, 3)
    assert isinstance(day.columns[1], bytes)
    assert not isinstance(day.columns[0], bytes)


def test_columnar_round_trip_preserves_times_ends_columns_and_guesses():
    records = [
        change(1, g0=10, g17=None),
        dayfile.Guess(START + 1, "present", 18, 1),
        change(3.5, g0=12),
        dayfile.Guess(START + 4, None, 0, 0),
        dayfile.End(START + 9),
    ]
    day = dayfile.day_from_log(records, None, START, END)
    again = dayfile.decode_day(dayfile.encode_day(day))
    assert list(again.times) == list(day.times) == [START + 1, START + 3.5]
    assert list(again.ends) == list(day.ends) == [START + 3.5, START + 9]
    assert again.vector(1) == vector(g0=12)
    assert again.guesses == day.guesses == [(START + 1, START + 4, "present", 18, 1)]


def test_carry_continues_previous_day_vector_from_midnight():
    carry = dayfile.Carry(vector(g0=30), (START - 5, None, "not_present", 19, 1))
    day = dayfile.day_from_log([change(60, g0=31)], carry, START, END)
    assert list(day.times) == [START, START + 60]
    assert list(day.ends) == [START + 60, END]
    assert day.vector(0) == vector(g0=30)
    assert day.guesses == [(START, END, "not_present", 19, 1)]
    assert dayfile.carry_of(day) == dayfile.Carry(
        vector(g0=31), (START, None, "not_present", 19, 1)
    )


def test_corrupt_finished_file_is_rejected():
    with pytest.raises(ValueError):
        dayfile.decode_day(b"not a day file")


def test_out_of_order_records_are_rejected():
    with pytest.raises(ValueError):
        dayfile.encode_records([change(5, g0=1), change(4, g0=2)], START)


def test_invalid_values_are_rejected():
    for value in (math.nan, -1, 101):
        with pytest.raises(ValueError):
            dayfile.encode_records([change(1, g0=value)], START)


def test_every_record_kind_round_trips_through_the_log():
    records = [
        change(0, g0=None, g1=12.5, g2=100),
        dayfile.Guess(START + 3600, "not_present", 19, 2),
        dayfile.End(START + 80_000),
    ]
    data = dayfile.encode_records(records, START)
    assert dayfile.decode_log(data, START) == records
    assert dayfile.decode_log(data[:6], START) == []  # Cut inside the first payload.


def test_damaged_finished_files_are_rejected():
    import struct
    import zlib

    good = dayfile.encode_day(dayfile.day_from_log([change(1, g0=1)], None, START, END))
    raw = zlib.decompress(good[4:])
    for damaged in (
        dayfile.MAGIC + b"garbage",
        dayfile.MAGIC + zlib.compress(raw + b"\0"),
        dayfile.MAGIC + zlib.compress(struct.pack("<B", 9) + raw[1:]),
    ):
        with pytest.raises(ValueError):
            dayfile.decode_day(damaged)


def test_guess_span_fills_only_the_unguessed_part_of_a_past_interval():
    records = [
        change(1, g0=5),
        dayfile.Guess(START + 10, "present", 18, 1),
        dayfile.Guess(START + 20, None, 0, 0),
        dayfile.GuessSpan(START + 40, START + 5, START + 30, "present", 18, 2),
    ]
    data = dayfile.encode_records(records, START)
    assert dayfile.decode_log(data, START) == records
    day = dayfile.day_from_log(records, None, START, END)
    assert day.guesses == [
        (START + 5, START + 10, "present", 18, 2),
        (START + 10, START + 20, "present", 18, 1),
        (START + 20, START + 30, "present", 18, 2),
    ]


def test_consecutive_guess_spans_join_and_spans_before_the_day_are_clipped():
    records = [
        change(1, g0=5),
        dayfile.GuessSpan(START + 20, START - 50, START + 10, "present", 18, 2),
        dayfile.GuessSpan(START + 30, START + 10, START + 15, "present", 18, 2),
    ]
    day = dayfile.day_from_log(records, None, START, END)
    assert day.guesses == [(START, START + 15, "present", 18, 2)]


def test_guess_span_never_overwrites_the_open_run():
    records = [
        change(1, g0=5),
        dayfile.Guess(START + 10, "not_present", 18, 1),
        dayfile.GuessSpan(START + 30, START + 2, START + 25, "present", 18, 2),
    ]
    day = dayfile.day_from_log(records, None, START, END)
    assert day.guesses == [
        (START + 2, START + 10, "present", 18, 2),
        (START + 10, END, "not_present", 18, 1),
    ]


def test_a_guess_span_reaching_past_its_own_time_is_rejected():
    with pytest.raises(ValueError):
        dayfile.encode_records(
            [dayfile.GuessSpan(START + 10, START + 5, START + 11, "present", 18, 2)], START
        )

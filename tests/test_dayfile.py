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


def gapped_day():
    return dayfile.day_from_log(
        [
            change(1.125, g0=4),
            change(3.625, g0=8),
            dayfile.End(START + 9.125),
            change(50.125, g0=12),
            dayfile.End(START + 60.125),
        ],
        None,
        START,
        END,
    )


def unplaned(raw, position, count):
    """The int32 values stored as four byte planes (low bytes first)."""
    import struct

    joined = bytearray(4 * count)
    for k in range(4):
        joined[k::4] = raw[position + k * count : position + (k + 1) * count]
    return struct.unpack(f"<{count}i", joined)


def test_v3_stores_time_steps_as_byte_planes_and_ends_beside_the_next_start():
    import zlib

    day = gapped_day()
    encoded = dayfile.encode_day(day)
    raw = zlib.decompress(encoded[4:])
    assert raw[0] == 3, "new files use version 3"
    size = dayfile.HEADER.size
    assert unplaned(raw, size, 3) == (1125, 2500, 46500)  # Steps between row starts.
    # A row's end as its distance from the next row's start: 0 when it runs on into it.
    assert unplaned(raw, size + 12, 3) == (0, 5500 - 46500, 10000)
    assert dayfile.decode_day(encoded) == day


def test_v2_known_answer_remains_readable():
    import struct
    import zlib

    header = dayfile.HEADER.pack(2, round(START * 1000), round(END * 1000), 3, 0, *([0] * 18))
    body = struct.pack("<6i", 1125, 2500, 46500, 2500, 5500, 10000)
    body += bytes([4, 8, 12]) + bytes([255] * 3) * 17
    day = dayfile.decode_day(dayfile.MAGIC + zlib.compress(header + body))
    assert day == gapped_day()


def test_v3_is_much_smaller_than_v2_for_steadily_recorded_days():
    import random
    import zlib
    from array import array

    rng = random.Random(3)
    builder = dayfile.DayBuilder(START, END)
    t = 0.0
    for _ in range(20000):
        t += rng.choice((0.4, 0.9, 1.3, 2.0, 2.2)) + rng.randrange(300) / 1000  # Jitter.
        builder.add(change(round(t, 3), **{f"g{g}": rng.randrange(30) for g in range(10)}))
    day = builder.day()
    starts = dayfile._offsets(day, day.times)
    v2 = dayfile.HEADER.pack(2, round(START * 1000), round(END * 1000), len(starts), 0, *([0] * 18))
    v2 += array("i", (b - a for a, b in zip((0, *starts), starts, strict=False))).tobytes()
    v2 += array(
        "i", (e - s for s, e in zip(starts, dayfile._offsets(day, day.ends), strict=True))
    ).tobytes()
    v2 += b"".join(day.columns)
    encoded = dayfile.encode_day(day)
    assert len(encoded) < 0.8 * len(dayfile.MAGIC + zlib.compress(v2, 6))
    assert dayfile.decode_day(encoded) == day


def test_v1_known_answer_remains_readable():
    import struct
    import zlib

    header = dayfile.HEADER.pack(1, round(START * 1000), round(END * 1000), 2, 1, *([0] * 18))
    body = struct.pack("<4i", 1125, 50125, 9125, 60125)
    body += bytes([4, 12]) + bytes([255, 255]) * 17
    body += dayfile.RUN.pack(1125, 9125, 1, 18, 1)
    day = dayfile.decode_day(dayfile.MAGIC + zlib.compress(header + body))
    assert list(day.times) == [START + 1.125, START + 50.125]
    assert list(day.ends) == [START + 9.125, START + 60.125]
    assert day.vector(0) == vector(g0=4)
    assert day.guesses == [(START + 1.125, START + 9.125, "present", 18, 1)]


def test_delta_layout_compacts_large_history_without_dropping_events():
    import zlib

    builder = dayfile.DayBuilder(START, END)
    for index in range(12000):
        builder.add(change(index * 0.125, g0=index % 100))
    builder.add(dayfile.End(START + 1500))
    day = builder.day()
    legacy = dayfile.HEADER.pack(1, round(START * 1000), round(END * 1000), 12000, 0, *([0] * 18))
    legacy += dayfile._offsets(day, day.times).tobytes() + dayfile._offsets(day, day.ends).tobytes()
    legacy += b"".join(day.columns)
    encoded = dayfile.encode_day(day)
    assert len(encoded) < 0.7 * len(zlib.compress(legacy, 6))
    assert dayfile.decode_day(encoded) == day


def planes(*values):
    import struct

    raw = struct.pack(f"<{len(values)}i", *values)
    return b"".join(raw[k::4] for k in range(4))


@pytest.mark.parametrize(
    "steps,ends",
    [
        ((-1,), (1,)),  # A row before the day.
        ((0,), (-1,)),  # A row ending before it starts.
        ((86400000,), (1,)),  # A row past the day's end.
        ((0, 10), (2147483647, 0)),  # An end that overflows once its next start is added.
    ],
)
def test_invalid_v3_time_ranges_are_rejected(steps, ends):
    import zlib

    rows = len(steps)
    header = dayfile.HEADER.pack(3, 0, 86400000, rows, 0, *([0] * 18))
    body = planes(*steps) + planes(*ends) + bytes([255]) * 18 * rows
    with pytest.raises(ValueError):
        dayfile.decode_day(dayfile.MAGIC + zlib.compress(header + body))


@pytest.mark.parametrize("times", [(-1, 1), (0, -1), (86400000, 1)])
def test_invalid_v2_time_ranges_are_rejected(times):
    import struct
    import zlib

    header = dayfile.HEADER.pack(2, 0, 86400000, 1, 0, *([0] * 18))
    body = struct.pack("<2i", *times) + bytes([255]) * 18
    with pytest.raises(ValueError):
        dayfile.decode_day(dayfile.MAGIC + zlib.compress(header + body))


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


def test_live_views_clip_only_what_reaches_past_their_time():
    """A view at any time equals clipping every row's end, without a step per row."""
    import random

    rng = random.Random(41)
    builder = dayfile.DayBuilder(0, 86400)
    t = 0.0
    for _ in range(600):
        t += rng.choice((0.5, 1, 3, 7))
        if rng.random() < 0.08:
            builder.add(dayfile.End(t))
        else:
            builder.add(dayfile.Change(t, {rng.randrange(18): rng.choice((None, 3, 40, 99))}))
        until = t + rng.choice((0, 0.25, 2))
        view = builder.view(until)
        count = len(view.times)
        expected = [min(end, until) for end in builder.ends[:count]]
        if count and count == len(builder.times) and builder.current is not None:
            expected[-1] = until
        assert list(view.ends) == expected


def test_predecessor_before_day_start_keeps_legacy_lossless_encoding():
    import zlib
    from array import array

    day = dayfile.Day(100, 200, array("d", [99.5]), array("d", [101]), [bytes([10])] * 18, [])
    encoded = dayfile.encode_day(day)
    assert zlib.decompress(encoded[4:])[0] == 1
    assert dayfile.decode_day(encoded) == day


def test_rows_store_exactly_what_storing_each_gate_stores():
    """Rows of whole readings and missing gates take the builder's direct path; a fractional
    reading widens its gate's column to floats, before or after; the day is the same."""
    import random

    def slow(builder, time, vector):  # The builder's per-gate path.
        if builder.current is not None:
            builder.ends[-1] = time
        builder.times.append(time)
        builder.ends.append(time)
        for gate, value in enumerate(vector):
            builder._store(gate, value, None)
        builder.current = vector

    rnd = random.Random(3)
    for trial in range(30):
        records, t = [], START
        for _ in range(200):
            t += rnd.choice((0, 1, 6))  # Some at the same time: a change of the last row.
            values = {
                gate: rnd.choice((None, 0, 7, 99, 12.0) + ((3.5,) if trial % 3 == 0 else ()))
                for gate in rnd.sample(range(18), rnd.randint(1, 18))
            }
            records.append(dayfile.Change(t, values))
            if rnd.random() < 0.05:
                records.append(dayfile.End(t + 2))
        fast = dayfile.DayBuilder(START, START + 86_400)
        reference = dayfile.DayBuilder(START, START + 86_400)
        reference._row = lambda time, vector, b=reference: slow(b, time, vector)
        for record in records:
            fast.add(record)
            reference.add(record)
        assert dayfile.encode_day(fast.day()) == dayfile.encode_day(reference.day())

"""One radar's recorded day: the live day's append-only log and the finished day's columns.

Times have millisecond resolution (``Change`` and the other records normalise them). A log is
a sequence of records, each a kind byte and a varint millisecond step from the previous one:
a ``Change`` of some gates (a 3-byte gate mask, then per changed gate one byte 0..100, 255
for a missing reading, or 254 and a float32), an ``End`` of coverage, or a ``Guess`` that
starts an automatic guess run. A truncated last record (a crash mid-append) is ignored.

A finished ``Day`` holds a row per distinct vector: its time, the end of its coverage and, per
gate, a column with the full vector (bytes, 255 missing, or float32, NaN missing, for a gate
that read a non-integer that day), plus the day's guess runs ``(start, end, state, level,
basis)``. ``encode_day`` stores it zlib-compressed.
"""

import math
import struct
import zlib
from array import array
from bisect import bisect_right
from dataclasses import dataclass, field
from itertools import repeat
from operator import add, mul, sub, truediv

GATES = 18
MISSING, FLOAT = 255, 254
CHANGE, END, GUESS, SPAN = range(4)
STATES = (None, "present", "not_present")
MAGIC, VERSION = b"LDH1", 1
HEADER = struct.Struct("<BqqII18B")  # version, start ms, end ms, rows, guesses, float gates.
RUN = struct.Struct("<iiBBB")  # start, end (ms from day start), state code, level, basis.


def _ms(seconds):
    return round(seconds * 1000)


def _seconds(ms):
    return ms / 1000


@dataclass(frozen=True)
class Change:
    time: float
    values: dict = field(compare=True)

    def __post_init__(self):
        object.__setattr__(self, "time", _seconds(_ms(self.time)))


@dataclass(frozen=True)
class End:
    time: float

    def __post_init__(self):
        object.__setattr__(self, "time", _seconds(_ms(self.time)))


@dataclass(frozen=True)
class Guess:
    time: float
    state: str | None
    level: int
    basis: int

    def __post_init__(self):
        object.__setattr__(self, "time", _seconds(_ms(self.time)))


@dataclass(frozen=True)
class GuessSpan:
    """A guess, recorded at ``time``, for the earlier ``[start, end)`` where the radar had none
    (an external source confirming a buffered period). It never replaces a recorded guess."""

    time: float
    start: float
    end: float
    state: str
    level: int
    basis: int

    def __post_init__(self):
        for name in ("time", "start", "end"):
            object.__setattr__(self, name, _seconds(_ms(getattr(self, name))))


@dataclass(frozen=True)
class Carry:
    """What holds at a day's start: the vector (None: no coverage) and the open guess run."""

    vector: tuple | None
    run: tuple | None


@dataclass
class Day:
    start: float
    end: float
    times: array
    ends: array
    columns: list
    guesses: list

    def vector(self, index):
        return tuple(_read(column, index) for column in self.columns)


def _read(column, index):
    value = column[index]
    if isinstance(column, bytes):
        return None if value == MISSING else value
    return None if math.isnan(value) else value


# --------------------------------------------------------------------------- log records


def _varint(value):
    out = bytearray()
    while True:
        byte, value = value & 0x7F, value >> 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return out


def _valid(value):
    if value is not None and not (math.isfinite(value) and 0 <= value <= 100):
        raise ValueError(f"Energy reading out of range: {value!r}")
    return value


def _change_payload(values):
    mask, payload = 0, bytearray()
    for gate in sorted(values):
        mask |= 1 << gate
        value = _valid(values[gate])
        if value is None:
            payload.append(MISSING)
        elif value == int(value):
            payload.append(int(value))
        else:
            payload.append(FLOAT)
            payload += struct.pack("<f", value)
    return mask.to_bytes(3, "little") + payload


def _payload(record):
    if isinstance(record, Change):
        return CHANGE, _change_payload(record.values)
    if isinstance(record, End):
        return END, b""
    fields = bytes((STATES.index(record.state), record.level, record.basis))
    if isinstance(record, GuessSpan):
        now, start, end = _ms(record.time), _ms(record.start), _ms(record.end)
        if not start <= end <= now:
            raise ValueError("A guess span must end by the time it is recorded")
        return SPAN, _varint(now - start) + _varint(now - end) + fields
    return GUESS, fields


def encode_records(records, previous):
    """Records appended after ``previous`` (seconds: the day start or the last record)."""
    out, last = bytearray(), _ms(previous)
    for record in records:
        now = _ms(record.time)
        if now < last:
            raise ValueError("Recorded times must not go backwards")
        kind, payload = _payload(record)
        out.append(kind)
        out += _varint(now - last)
        out += payload
        last = now
    return bytes(out)


class _Reader:
    def __init__(self, data):
        self.data, self.position = data, 0

    def byte(self):
        value = self.data[self.position]
        self.position += 1
        return value

    def varint(self):
        value, shift = 0, 0
        while True:
            byte = self.byte()
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return value
            shift += 7

    def take(self, count):
        if self.position + count > len(self.data):
            raise IndexError("truncated")
        chunk = self.data[self.position : self.position + count]
        self.position += count
        return chunk


def _read_change(reader, time):
    mask, values = int.from_bytes(reader.take(3), "little"), {}
    for gate in range(GATES):
        if mask >> gate & 1:
            code = reader.byte()
            values[gate] = (
                None
                if code == MISSING
                else struct.unpack("<f", reader.take(4))[0]
                if code == FLOAT
                else code
            )
    return Change(time, values)


def _read_record(reader, last):
    kind = reader.byte()
    now = last + reader.varint()
    time = _seconds(now)
    if kind == CHANGE:
        return now, _read_change(reader, time)
    if kind == END:
        return now, End(time)
    if kind == SPAN:
        start, end = _seconds(now - reader.varint()), _seconds(now - reader.varint())
        state, level, basis = reader.take(3)
        return now, GuessSpan(time, start, end, STATES[state], level, basis)
    state, level, basis = reader.take(3)
    return now, Guess(time, STATES[state], level, basis)


def decode_log(data, previous):
    """Every complete record; a truncated tail (a crash mid-append) is dropped."""
    reader, last, records = _Reader(data), _ms(previous), []
    while reader.position < len(data):
        try:
            last, record = _read_record(reader, last)
        except (IndexError, struct.error):
            break
        records.append(record)
    return records


# --------------------------------------------------------------------------- days


class DayBuilder:
    """A day built record by record into its columns (a few bytes a row, no row objects), so
    the live day can stay open in memory; ``view`` reads it without closing it."""

    def __init__(self, start, end, carry=None):
        self.start, self.end = start, end
        self._summary_version = 0
        self.times, self.ends, self.guesses = array("d"), array("d"), []
        self.confirmed = []  # (recorded at, run) from guess spans: later records about earlier time.
        self.columns = [bytearray() for _ in range(GATES)]
        self.current, self.run = None, None
        if carry is not None and carry.vector is not None:
            self._row(start, list(carry.vector))
        if carry is not None and carry.run is not None:
            self.run = (start, *carry.run[2:])

    def add(self, record):
        self._summary_version += 1
        if isinstance(record, Change):
            self._change(record)
        elif isinstance(record, End):
            self._close(record.time)
        elif isinstance(record, GuessSpan):
            self._splice(record)
        else:
            self._guess(record.time, record)

    def _row(self, time, vector):
        if self.current is not None:
            self.ends[-1] = time
        self.times.append(time)
        self.ends.append(time)
        for gate, value in enumerate(vector):
            self._store(gate, value, None)
        self.current = vector

    def _store(self, gate, value, index):
        column = self._column(gate, value)
        code = _code(column, value)
        if index is None:
            column.append(code)
        else:
            column[index] = code

    def _column(self, gate, value):
        """The gate's column, widened to float32 the first time it reads a non-integer."""
        column = self.columns[gate]
        if isinstance(column, bytearray) and value is not None and value != int(value):
            column = self.columns[gate] = array(
                "f", (math.nan if byte == MISSING else byte for byte in column)
            )
        return column

    def _change(self, record):
        if self.current is not None and self.times[-1] == record.time:
            for gate, value in record.values.items():
                self.current[gate] = value
                self._store(gate, value, len(self.times) - 1)
            return
        vector = list(self.current) if self.current is not None else [None] * GATES
        for gate, value in record.values.items():
            vector[gate] = value
        if vector != self.current:
            self._row(record.time, vector)

    def _close(self, time):
        if self.current is not None:
            self.ends[-1] = time
        self.current = None
        self._guess(time, None)

    def _guess(self, time, record):
        if self.run is not None and self.run[0] < time:
            self.guesses.append((self.run[0], time, *self.run[1:]))
        self.run = None
        if record is not None and record.state is not None:
            self.run = (time, record.state, record.level, record.basis)

    def _splice(self, record):
        """Fill the unguessed part of the span's interval within this day."""
        start, end = max(record.start, self.start), min(record.end, self.end)
        covered = [(run[0], run[1]) for run in self.guesses]
        covered += [(run[0], run[1]) for _recorded, run in self.confirmed]
        if self.run is not None:
            covered.append((self.run[0], math.inf))
        added = [
            (low, high, record.state, record.level, record.basis)
            for low, high in gaps(start, end, sorted(covered))
        ]
        self.confirmed += [(record.time, run) for run in added]

    def view(self, until, recorded=None):
        """The day as of ``until``: rows and runs starting after it are left out, and what is
        still open at ``until`` ends there; guess spans count once recorded (by ``recorded``,
        default ``until``). The builder stays open."""
        recorded = until if recorded is None else recorded
        count = bisect_right(self.times, until)
        # A row ends where the next begins (or earlier), so only the last row up to ``until``
        # can reach past it: one copy, no step per row (the live day is viewed every poll).
        ends = self.ends[:count]
        if count:
            ends[-1] = (
                until
                if count == len(self.times) and self.current is not None
                else min(ends[-1], until)
            )
        runs = [
            (start, min(end, until), *rest)
            for start, end, *rest in _joined(
                sorted(self.guesses + [run for at, run in self.confirmed if at <= recorded])
            )
            if start <= until
        ]
        if self.run is not None and self.run[0] <= until:  # Started now: the current guess.
            runs.append((self.run[0], until, *self.run[1:]))
        columns = [
            bytes(c[:count]) if isinstance(c, bytearray) else array("f", c[:count])
            for c in self.columns
        ]
        day = Day(self.start, self.end, array("d", self.times[:count]), ends, columns, runs)
        day._summary_owner = self
        day._summary_version = self._summary_version
        return day

    def day(self):
        """The finished day: anything still open is covered to the day's end, with every guess
        span recorded about it (a span recorded after midnight about this day included)."""
        return self.view(self.end, math.inf)


def gaps(start, end, covered):
    """The parts of ``[start, end)`` outside the sorted ``covered`` intervals."""
    for low, high in covered:
        if low > start:
            yield start, min(low, end)
        start = max(start, high)
        if start >= end:
            return
    if start < end:
        yield start, end


def splice(day, spans):
    """``day`` with each guess span filling only its unguessed time within the day (a span
    recorded on a later day about this one). Applying a span twice adds nothing."""
    covered = [(run[0], run[1]) for run in day.guesses]
    added = []
    for span in spans:
        start, end = max(span.start, day.start), min(span.end, day.end)
        for low, high in list(gaps(start, end, sorted(covered))):
            added.append((low, high, span.state, span.level, span.basis))
            covered.append((low, high))
    if not added:
        return day
    return Day(
        day.start, day.end, day.times, day.ends, day.columns, _joined(sorted(day.guesses + added))
    )


def _joined(runs):
    """Neighbouring runs of one guess that meet become one."""
    out = []
    for run in runs:
        if out and out[-1][1] == run[0] and out[-1][2:] == run[2:]:
            out[-1] = (out[-1][0], run[1], *run[2:])
        else:
            out.append(run)
    return out


def _code(column, value):
    if isinstance(column, bytearray):
        return MISSING if value is None else int(value)
    return math.nan if value is None else value


def day_from_log(records, carry, start, end):
    """The day ``[start, end)`` from its records, continuing ``carry`` from its start."""
    builder = DayBuilder(start, end, carry)
    for record in records:
        builder.add(record)
    return builder.day()


def carry_of(day):
    """What continues into the next day: the vector and guess run still open at its end."""
    open_row = len(day.times) and day.ends[-1] == day.end
    open_run = day.guesses and day.guesses[-1][1] == day.end
    return Carry(
        day.vector(len(day.times) - 1) if open_row else None,
        (day.guesses[-1][0], None, *day.guesses[-1][2:]) if open_run else None,
    )


# --------------------------------------------------------------------------- finished files


def _offsets(day, times):
    """``_ms(time) - _ms(day.start)`` for every time, without a Python step per row."""
    milliseconds = map(round, map(mul, times, repeat(1000)))
    return array("i", map(sub, milliseconds, repeat(_ms(day.start))))


def encode_day(day):
    floats = [0 if isinstance(column, bytes) else 1 for column in day.columns]
    body = bytearray(
        HEADER.pack(
            VERSION, _ms(day.start), _ms(day.end), len(day.times), len(day.guesses), *floats
        )
    )
    body += _offsets(day, day.times).tobytes() + _offsets(day, day.ends).tobytes()
    for column in day.columns:
        body += column if isinstance(column, bytes) else column.tobytes()
    base = _ms(day.start)
    for start, end, state, level, basis in day.guesses:
        body += RUN.pack(_ms(start) - base, _ms(end) - base, STATES.index(state), level, basis)
    return MAGIC + zlib.compress(bytes(body), 6)


def _times(raw, position, count, base):
    offsets = array("i")
    offsets.frombytes(raw[position : position + 4 * count])
    # ``_seconds(base + offset)`` for every offset, without a Python step per row.
    seconds = map(truediv, map(add, offsets, repeat(base)), repeat(1000))
    return array("d", seconds), position + 4 * count


def _columns(raw, position, count, floats):
    columns = []
    for is_float in floats:
        size = count * (4 if is_float else 1)
        chunk = raw[position : position + size]
        if is_float:
            column = array("f")
            column.frombytes(chunk)
            columns.append(column)
        else:
            columns.append(bytes(chunk))
        position += size
    return columns, position


def _runs(raw, position, count, base):
    runs = []
    for _ in range(count):
        start, end, state, level, basis = RUN.unpack_from(raw, position)
        runs.append((_seconds(base + start), _seconds(base + end), STATES[state], level, basis))
        position += RUN.size
    return runs, position


def decode_day(data):
    if data[:4] != MAGIC:
        raise ValueError("Not a recorded day file")
    try:
        raw = zlib.decompress(data[4:])
        version, start, end, count, runs, *floats = HEADER.unpack_from(raw)
        if version != VERSION:
            raise ValueError(f"Unsupported day file version {version}")
        times, position = _times(raw, HEADER.size, count, start)
        ends, position = _times(raw, position, count, start)
        columns, position = _columns(raw, position, count, floats)
        guesses, position = _runs(raw, position, runs, start)
    except (zlib.error, struct.error, IndexError) as error:
        raise ValueError("Corrupt day file") from error
    if position != len(raw):
        raise ValueError("Corrupt day file")
    return Day(_seconds(start), _seconds(end), times, ends, columns, guesses)

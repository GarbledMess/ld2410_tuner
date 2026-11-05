"""Finished history must be bounded before decompression or column allocation."""

import zlib

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.history import dayfile
from tuner_under_test.worker import history


def test_time_index_writes_one_buffer_per_block():
    import io
    import struct

    class Index(io.BytesIO):
        writes = 0

        def write(self, data):
            self.writes += 1
            return super().write(data)

    reader = object.__new__(history._ColumnReader)
    reader.start, reader.end, reader.index = 1790000000000, 1790086400000, Index()
    starts = list(range(0, 24000, 6))
    ends = [value + 5 for value in starts]
    assert reader._write_times(starts, ends, None) == ends[-1]
    assert reader.index.getvalue() == b"".join(
        struct.pack("<dd", (reader.start + a) / 1000, (reader.start + b) / 1000)
        for a, b in zip(starts, ends, strict=True)
    )
    assert reader.index.writes == 1


def test_column_blocks_decode_time_arrays_without_per_row_generators():
    import io
    import struct
    import sys
    from array import array

    reader = object.__new__(history._ColumnReader)
    reader.count = 9000
    reader.index = io.BytesIO(
        b"".join(struct.pack("<dd", i * 6, i * 6 + 5) for i in range(reader.count))
    )
    reader.raw = io.BytesIO(bytes([20]) * reader.count * 18)
    reader.flags = [0] * 18
    reader.offsets = [i * reader.count for i in range(18)]
    calls = []

    def profile(frame, event, _arg):
        if event == "call" and frame.f_code.co_filename == history.__file__:
            calls.append(frame.f_code.co_name)

    sys.setprofile(profile)
    try:
        blocks = list(reader.blocks(2, 53995, (0, 17)))
    finally:
        sys.setprofile(None)
    times = array("d", (value for block in blocks for value in block.times))
    ends = array("d", (value for block in blocks for value in block.ends))
    assert times == array("d", [2, *range(6, 54000, 6)])
    assert ends == array("d", [*range(5, 53999, 6), 53995])
    assert all(block.columns[0] == bytes([20]) * len(block.times) for block in blocks)
    assert all(block.columns[1] == bytes([255]) * len(block.times) for block in blocks)
    assert calls.count("<genexpr>") <= 60


@pytest.mark.parametrize("planes", [False, True])
def test_integer_columns_decode_in_bulk(planes, monkeypatch):
    import io
    import struct
    from types import SimpleNamespace

    values = [-2147483648, -1, 0, 1, 2147483647] * 100
    raw = struct.pack("<" + "i" * len(values), *values)
    if planes:
        raw = b"".join(raw[i::4] for i in range(4))
    calls = []

    def counted(*args):
        calls.append(1)
        return struct.iter_unpack(*args)

    monkeypatch.setattr(history, "struct", SimpleNamespace(iter_unpack=counted))
    assert history._integers(io.BytesIO(raw), 0, len(values), 2, 100, planes) == values[2:102]
    assert not calls


def encoded(rows=0, runs=0, flags=None, body=b""):
    header = dayfile.HEADER.pack(1, 0, 86400000, rows, runs, *(flags or [0] * 18))
    return dayfile.MAGIC + zlib.compress(header + body)


def test_empty_day_roundtrip():
    result = history.decode(encoded())
    assert result.start == 0
    assert result.end == 86400
    assert not result.times
    assert result.columns == [b""] * 18


def test_v2_known_answer_and_count_limits_before_columns(monkeypatch):
    import struct

    header = dayfile.HEADER.pack(2, 0, 86400000, 2, 0, *([0] * 18))
    body = struct.pack("<4i", 1125, 49000, 8000, 10000) + bytes([4, 12]) + bytes([255, 255]) * 17
    payload = dayfile.MAGIC + zlib.compress(header + body)
    result = history.decode(payload)
    assert list(result.times) == [1.125, 50.125]
    assert list(result.ends) == [9.125, 60.125]
    assert result.vector(0)[0] == 4
    monkeypatch.setattr(dayfile, "_columns", lambda *args: pytest.fail("allocated columns"))
    with pytest.raises(history.HistoryRejected, match="count budget"):
        history.decode(payload, max_rows=1)


def test_v3_files_decode_like_the_day_they_were_made_from():
    builder = dayfile.DayBuilder(0, 86400)
    for second, value in ((1.125, 4), (3.625, 8), (50.125, 12)):
        builder.add(dayfile.Change(second, {0: value}))
    builder.add(dayfile.End(60.125))
    day = builder.day()
    payload = dayfile.encode_day(day)
    assert zlib.decompress(payload[4:])[0] == 3
    assert history.decode(payload) == day


def test_v3_invalid_times_refused_before_columns(monkeypatch):
    import struct

    header = dayfile.HEADER.pack(3, 0, 86400000, 1, 0, *([0] * 18))
    ends = struct.pack("<i", -1)  # Ends before it starts.
    planes = b"\x00" * 4 + b"".join(ends[k::4] for k in range(4))
    payload = dayfile.MAGIC + zlib.compress(header + planes + bytes([255]) * 18)
    monkeypatch.setattr(dayfile, "_columns", lambda *args: pytest.fail("allocated columns"))
    with pytest.raises(history.HistoryRejected):
        history.decode(payload)


@pytest.mark.parametrize("times", [(-1, 1), (0, -1), (86400000, 1)])
def test_v2_invalid_times_refused_before_columns(times, monkeypatch):
    import struct

    header = dayfile.HEADER.pack(2, 0, 86400000, 1, 0, *([0] * 18))
    payload = dayfile.MAGIC + zlib.compress(header + struct.pack("<2i", *times) + bytes([255]) * 18)
    monkeypatch.setattr(dayfile, "_columns", lambda *args: pytest.fail("allocated columns"))
    with pytest.raises(history.HistoryRejected):
        history.decode(payload)


@pytest.mark.parametrize("payload", [encoded(rows=32769), encoded(runs=4097)])
def test_counts_rejected_before_columns(payload, monkeypatch):
    monkeypatch.setattr(dayfile, "_columns", lambda *args: pytest.fail("allocated columns"))
    with pytest.raises(history.HistoryRejected):
        history.decode(payload, max_rows=32768, max_guesses=4096)


def test_expansion_bomb_is_bounded():
    payload = dayfile.MAGIC + zlib.compress(b"x" * 100000)
    with pytest.raises(history.HistoryRejected):
        history.decode(payload, max_raw_bytes=100)


@pytest.mark.parametrize(
    "payload",
    [b"bad", encoded()[:-1], encoded() + b"junk", encoded(flags=[2] * 18), encoded(body=b"extra")],
)
def test_malformed_payload_rejected(payload):
    with pytest.raises(history.HistoryRejected):
        history.decode(payload)


def test_compressed_input_cap():
    with pytest.raises(history.HistoryRejected):
        history.decode(encoded(), max_compressed_bytes=1)


def test_legacy_float_columns_and_guesses_preserved():
    from array import array

    original = dayfile.Day(
        0,
        86400,
        array("d", [1]),
        array("d", [2]),
        [array("f", [1.25])] + [bytes([255])] * 17,
        [(1, 2, "present", 2, 0)],
    )
    assert history.decode(dayfile.encode_day(original)) == original


def test_specific_budget_refusal_is_not_reported_as_corrupt_history():
    with pytest.raises(history.HistoryRejected, match="History count budget exceeded"):
        history.decode(encoded(rows=32769), max_rows=32768)


@pytest.mark.parametrize(
    "option", ["max_rows", "max_guesses", "max_raw_bytes", "max_compressed_bytes"]
)
@pytest.mark.parametrize("value", [-1, True, 1.5])
def test_invalid_budgets(option, value):
    with pytest.raises(ValueError):
        history.decode(encoded(), **{option: value})


def reference(path):
    from tuner_under_test.history.worker_snapshot import FileReference

    found = path.stat()
    return FileReference(path, (found.st_dev, found.st_ino, found.st_mtime_ns, found.st_size))


def test_read_reference_roundtrip(tmp_path):
    path = tmp_path / "day"
    path.write_bytes(encoded())
    assert history.read(reference(path)).end == 86400


def test_changed_reference_is_rejected_before_decode(tmp_path, monkeypatch):
    path = tmp_path / "day"
    path.write_bytes(encoded())
    saved = reference(path)
    path.write_bytes(b"changed")
    monkeypatch.setattr(
        history, "decode", lambda *args, **kwargs: pytest.fail("decoded stale file")
    )
    with pytest.raises(history.HistoryRejected):
        history.read(saved)


def test_file_size_checked_before_read(tmp_path):
    path = tmp_path / "day"
    path.write_bytes(encoded())
    with pytest.raises(history.HistoryRejected):
        history.read(reference(path), max_compressed_bytes=1)


def test_replaced_path_during_read_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "day"
    path.write_bytes(encoded())
    saved = reference(path)
    original = type(path).stat

    def replacing(self, *args, **kwargs):
        if self == path:
            path.unlink()
            path.write_bytes(encoded(body=b"replacement"))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(path), "stat", replacing)
    with pytest.raises(history.HistoryRejected):
        history.read(saved)


def test_changed_open_file_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "day"
    path.write_bytes(encoded())
    saved = reference(path)
    original = history._identity
    calls = []

    def changed(found):
        calls.append(found)
        return original(found) if len(calls) == 1 else (0, 0, 0, 0)

    monkeypatch.setattr(history, "_identity", changed)
    with pytest.raises(history.HistoryRejected):
        history.read(saved)


@pytest.mark.parametrize("value", [-1, True, 1.5])
def test_read_invalid_budget(tmp_path, value):
    with pytest.raises(ValueError):
        history.read(None, max_compressed_bytes=value)


def test_zero_expansion_budget_still_has_a_limit():
    with pytest.raises(history.HistoryRejected):
        history.decode(encoded(), max_raw_bytes=0)


@pytest.mark.parametrize("version,start,end", [(4, 0, 10), (0, 0, 10), (1, 10, 0)])
def test_invalid_header(version, start, end):
    payload = dayfile.MAGIC + zlib.compress(
        dayfile.HEADER.pack(version, start, end, 0, 0, *([0] * 18))
    )
    with pytest.raises(history.HistoryRejected):
        history.decode(payload)


def test_nonregular_replacement_cannot_block(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    root = Path(dayfile.__file__).resolve().parents[1]
    code = f"""
import os, sys, types
from pathlib import Path
package = types.ModuleType("offline_tuner")
package.__path__ = [{str(root)!r}]
sys.modules[package.__name__] = package
from offline_tuner.worker import history
from offline_tuner.history.worker_snapshot import FileReference
path = Path({str(tmp_path / "replaced")!r})
path.write_bytes(b"regular")
found = path.stat()
reference = FileReference(path, (found.st_dev, found.st_ino, found.st_mtime_ns, found.st_size))
path.unlink()
if hasattr(os, "mkfifo"):
    os.mkfifo(path)
else:
    path.mkdir()
try:
    history.read(reference)
except (OSError, history.HistoryRejected):
    print("nonregular input rejected")
else:
    raise AssertionError("nonregular input accepted")
"""
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", code], capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "nonregular input rejected"


def test_v2_accumulated_timestamp_overflow_is_rejected_before_columns(monkeypatch):
    import struct

    header = dayfile.HEADER.pack(2, 0, 86400000, 2, 0, *([0] * 18))
    body = struct.pack("<4i", 2147483647, 2147483647, 0, 0) + bytes([255]) * 36
    monkeypatch.setattr(dayfile, "_columns", lambda *args: pytest.fail("allocated columns"))
    with pytest.raises(history.HistoryRejected, match="Invalid or oversized") as caught:
        history.decode(dayfile.MAGIC + zlib.compress(header + body))
    assert str(caught.value.__cause__) == "Invalid delta timestamps"


@pytest.mark.parametrize("count", [0, 1, 4096])
def test_timestamp_index_interleaves_columns_without_row_tuples(monkeypatch, count):
    import builtins
    import struct
    from io import BytesIO

    starts = [i * 6 for i in range(count)]
    ends = [i * 6 + 5 for i in range(count)]
    reader = history._ColumnReader.__new__(history._ColumnReader)
    reader.start, reader.end = 1790000000000, 1790000100000
    reader.index = BytesIO()
    tuples = []

    def counted_zip(*args, strict=False):
        for row in builtins.zip(*args, strict=strict):
            tuples.append(1)
            yield row

    monkeypatch.setattr(history, "zip", counted_zip, raising=False)
    result = reader._write_times(starts, ends, None)
    expected = b"".join(
        struct.pack("<dd", (reader.start + lo) / 1000, (reader.start + hi) / 1000)
        for lo, hi in builtins.zip(starts, ends, strict=True)
    )
    assert reader.index.getvalue() == expected
    assert result == (ends[-1] if ends else None)
    assert not tuples

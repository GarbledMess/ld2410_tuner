"""Finished history must be bounded before decompression or column allocation."""

import zlib

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.history import dayfile
from tuner_under_test.worker import history


def encoded(rows=0, runs=0, flags=None, body=b""):
    header = dayfile.HEADER.pack(1, 0, 86400000, rows, runs, *(flags or [0] * 18))
    return dayfile.MAGIC + zlib.compress(header + body)


def test_empty_day_roundtrip():
    result = history.decode(encoded())
    assert result.start == 0
    assert result.end == 86400
    assert not result.times
    assert result.columns == [b""] * 18


@pytest.mark.parametrize("payload", [encoded(rows=32769), encoded(runs=4097)])
def test_counts_rejected_before_columns(payload, monkeypatch):
    monkeypatch.setattr(dayfile, "_columns", lambda *args: pytest.fail("allocated columns"))
    with pytest.raises(history.HistoryRejected):
        history.decode(payload)


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


@pytest.mark.parametrize("version,start,end", [(2, 0, 10), (1, 10, 0)])
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

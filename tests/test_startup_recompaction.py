"""Lossless finished-day upgrade stays inside the private startup transaction."""

import shutil
import struct
import zlib
from unittest.mock import patch

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.history import dayfile, daystore
from tuner_under_test.worker import startup

START = 1_760_486_400.0


def legacy_file(root):
    path = root / "radar" / "2025-10-15.day"
    path.parent.mkdir(parents=True)
    header = dayfile.HEADER.pack(
        1, round(START * 1000), round((START + 86400) * 1000), 2, 1, 1, *([0] * 17)
    )
    body = struct.pack("<4i", 1125, 50125, 9125, 60125)
    body += struct.pack("<2f", 4.125, 12.5) + bytes([255, 255]) * 17
    body += dayfile.RUN.pack(1125, 9125, 1, 18, 1)
    path.write_bytes(dayfile.MAGIC + zlib.compress(header + body))
    return path


def test_startup_recompacts_legacy_day_losslessly_without_finished_handoff(tmp_path):
    root = tmp_path / "history"
    path = legacy_file(root)
    original = dayfile.decode_day(path.read_bytes())
    with patch.object(daystore.DayStore, "day", side_effect=AssertionError("all-day restore")):
        result = startup.execute(
            {"root": str(root), "data": {"devices": {}}, "now": START + 2 * 86400}
        )
    assert zlib.decompress(path.read_bytes()[4:])[0] == dayfile.VERSION
    assert dayfile.decode_day(path.read_bytes()) == original
    assert result["live"] == []
    assert result["patches"] == {}


def test_startup_rewrites_version_2_days_into_the_smaller_version_3(tmp_path):
    root = tmp_path / "history"
    path = root / "radar" / "2025-10-15.day"
    path.parent.mkdir(parents=True)
    header = dayfile.HEADER.pack(
        2, round(START * 1000), round((START + 86400) * 1000), 2, 1, *([0] * 18)
    )
    body = struct.pack("<4i", 1125, 49000, 8000, 10000) + bytes([4, 12]) + bytes([255, 255]) * 17
    body += dayfile.RUN.pack(1125, 9125, 1, 18, 1)
    path.write_bytes(dayfile.MAGIC + zlib.compress(header + body))
    original = dayfile.decode_day(path.read_bytes())
    startup.execute({"root": str(root), "data": {"devices": {}}, "now": START + 2 * 86400})
    assert zlib.decompress(path.read_bytes()[4:])[0] == 3
    assert dayfile.decode_day(path.read_bytes()) == original


def test_current_day_encoding_is_not_rewritten(tmp_path):
    root = tmp_path / "history"
    path = legacy_file(root)
    path.write_bytes(dayfile.encode_day(dayfile.decode_day(path.read_bytes())))
    previous = path.stat().st_mtime_ns, path.read_bytes()
    with patch.object(dayfile, "encode_day", side_effect=AssertionError("repeated encode")):
        startup.execute({"root": str(root), "data": {"devices": {}}, "now": START + 2 * 86400})
    assert (path.stat().st_mtime_ns, path.read_bytes()) == previous


def test_failed_recompaction_never_modifies_original_owned_root(tmp_path):
    owned = tmp_path / "owned"
    original = legacy_file(owned)
    previous = original.read_bytes()
    clone = tmp_path / "job" / "history"
    shutil.copytree(owned, clone)
    with patch.object(dayfile, "encode_day", side_effect=ValueError("failed encoding")):
        with pytest.raises(ValueError, match="failed encoding"):
            startup.execute({"root": str(clone), "data": {"devices": {}}, "now": START + 2 * 86400})
    assert original.read_bytes() == previous
    assert (clone / "radar" / original.name).read_bytes() == previous


def test_changed_file_identity_cannot_be_overwritten_by_recompaction(tmp_path):
    from tuner_under_test.worker import history

    root = tmp_path / "history"
    path = legacy_file(root)
    encode = dayfile.encode_day

    def changed(day):
        path.write_bytes(b"changed while encoding")
        return encode(day)

    with patch.object(dayfile, "encode_day", side_effect=changed):
        with pytest.raises(history.HistoryRejected, match="changed during recompaction"):
            startup.execute({"root": str(root), "data": {"devices": {}}, "now": START + 2 * 86400})
    assert path.read_bytes() == b"changed while encoding"
    assert not list(path.parent.glob("*.recompact"))


def test_recompaction_rejects_invalid_counts_before_encoding(tmp_path):
    from tuner_under_test.worker import history

    root = tmp_path / "history"
    path = legacy_file(root)
    raw = bytearray(zlib.decompress(path.read_bytes()[4:]))
    struct.pack_into("<I", raw, 17, 100000000)
    path.write_bytes(dayfile.MAGIC + zlib.compress(raw))
    previous = path.read_bytes()
    with patch.object(dayfile, "encode_day", side_effect=AssertionError("unbounded decode")):
        with pytest.raises(history.HistoryRejected, match="payload length"):
            startup.execute({"root": str(root), "data": {"devices": {}}, "now": START + 2 * 86400})
    assert path.read_bytes() == previous


@pytest.mark.parametrize("payload", [b"invalid", dayfile.MAGIC + zlib.compress(b"\x09")])
def test_invalid_finished_file_cannot_be_recompacted(tmp_path, payload):
    from tuner_under_test.worker import history, recompaction

    path = tmp_path / "radar" / "2025-10-15.day"
    path.parent.mkdir()
    path.write_bytes(payload)
    with pytest.raises(history.HistoryRejected):
        recompaction.finished_days(tmp_path)
    assert path.read_bytes() == payload
    assert not list(path.parent.glob("*.recompact"))

"""Captured physical rows use synthetic wire files only."""

import zlib
from array import array
from datetime import date
from types import SimpleNamespace

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.history import dayfile
from tuner_under_test.history.fit_rows import CapturedRowSource
from tuner_under_test.history.worker_snapshot import FileReference
from tuner_under_test.worker import history
from tuner_under_test.worker.calculations import ReadOnlyDays


def payload(version=3, count=3, floating=False):
    starts = array("i", (i * 1000 for i in range(count)))
    ends = array("i", ((i + 1) * 1000 for i in range(count)))
    if version == 1:
        times = starts.tobytes() + ends.tobytes()
    elif version == 2:
        times = (
            array("i", [0] + [1000] * (count - 1)).tobytes() + array("i", [1000] * count).tobytes()
        )
    else:
        steps = array("i", [0] + [1000] * (count - 1))
        tails = array("i", [0] * (count - 1) + [1000])
        times = dayfile._planes(steps) + dayfile._planes(tails)
    flags = [int(floating)] + [0] * 17
    first = (
        array("f", [40.01, float("nan")] + [20] * (count - 2)).tobytes()
        if floating
        else bytes(i % 101 for i in range(count))
    )
    raw = dayfile.HEADER.pack(version, 0, 86400000, count, 0, *flags)
    return dayfile.MAGIC + zlib.compress(raw + times + first + bytes([255]) * count * 17)


def reference(path):
    s = path.stat()
    return FileReference(path, (s.st_dev, s.st_ino, s.st_mtime_ns, s.st_size))


def source(tmp_path, data=None, cutoff=float("inf")):
    path = tmp_path / "1970-01-01.day"
    path.write_bytes(payload() if data is None else data)
    ref = reference(path)
    request = {
        "days": [
            {
                "device_id": "synthetic",
                "date": "1970-01-01",
                "path": str(path),
                "identity": ref.identity,
                "late": [],
                "persistent": True,
            }
        ],
        "budgets": {"max_file_bytes": None},
    }
    days = ReadOnlyDays(request)
    view = SimpleNamespace(_days=days, _history_until=cutoff)
    return CapturedRowSource(view, "synthetic", (0,)), days, path


@pytest.mark.parametrize("version", [1, 2, 3])
def test_reader_matches_day_decoder_versions_1_2_3(tmp_path, version):
    data = payload(version)
    src, days, _ = source(tmp_path, data)
    expected = dayfile.decode_day(data)
    blocks = list(src.blocks(0, 86400))
    assert [t for b in blocks for t in b.times] == list(expected.times)
    assert [t for b in blocks for t in b.ends] == list(expected.ends)
    assert b"".join(b.columns[0] for b in blocks) == expected.columns[0]
    assert not days.shared.cache
    assert days.days("synthetic") == [date(1970, 1, 1)]


def test_reader_clips_stable_row_at_query_start(tmp_path):
    src, _, _ = source(tmp_path)
    (b,) = src.blocks(1.5, 2.25)
    assert list(b.times) == [1.5, 2]
    assert list(b.ends) == [2, 2.25]
    (b,) = src.blocks(1, 2)
    assert list(b.times) == [1]


def test_reader_rejects_changed_identity_before_and_after_read(tmp_path):
    src, _, path = source(tmp_path)
    path.write_bytes(payload(count=4))
    with pytest.raises(history.HistoryRejected):
        list(src.blocks(0, 10))
    src, _, path = source(tmp_path)
    iterator = src.blocks(0, 10)
    next(iterator)
    path.write_bytes(payload(count=4))
    with pytest.raises(history.HistoryRejected):
        list(iterator)


def test_reader_quantizes_fractional_columns_with_ceil(tmp_path):
    src, _, _ = source(tmp_path, payload(floating=True))
    (b,) = src.blocks(0, 10)
    assert b.columns[0] == bytes([41, 255, 20])
    assert b.columns[1] == bytes([255] * 3)


def test_reader_caps_blocks_under_dense_six_hour_window(tmp_path):
    src, _, _ = source(tmp_path, payload(count=10000))
    blocks = list(src.blocks(0, 21600))
    assert sum(len(b.times) for b in blocks) == 10000
    assert max(len(b.times) for b in blocks) <= 4096


def test_reader_uses_live_snapshot_cutoff(tmp_path):
    src, _, _ = source(tmp_path, cutoff=1.5)
    (b,) = src.blocks(0, 10)
    assert list(b.ends) == [1, 1.5]
    assert src.rows_in(0, 10) == 2


def test_row_budget_counts_overlapping_rows_once_per_region(tmp_path):
    src, _, _ = source(tmp_path)
    assert src.rows_in(0.5, 2.5) == 3
    assert src.rows_in(1, 2) == 1
    assert src.rows_in(3, 4) == 0


@pytest.mark.parametrize("alter", [lambda b: b[:-1], lambda b: b + b"junk", lambda b: b"bad"])
def test_malformed_or_trailing_stream_rejected(tmp_path, alter):
    src, _, _ = source(tmp_path, alter(payload()))
    with pytest.raises(history.HistoryRejected):
        list(src.blocks(0, 10))

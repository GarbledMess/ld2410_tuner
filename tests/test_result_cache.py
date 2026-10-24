"""Synthetic finished-day result persistence and strict admission."""

import asyncio
import importlib
import json
import os
import zlib
from array import array
from unittest.mock import patch

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.calibration.evidence_budget import Chunk
from tuner_under_test.history import summary_cache


def cache():
    return importlib.import_module("tuner_under_test.history.result_cache")


@pytest.fixture
def physical(tmp_path):
    path = tmp_path / "radar" / "synthetic.day"
    path.parent.mkdir()
    path.write_bytes(b"synthetic physical identity")
    return path, (*summary_cache.stamp(path), ())


VALUES = [
    ("rows", 0),
    ("rows", 123),
    ("chunks", None),
    ("chunks", ()),
    (
        "chunks",
        (
            Chunk(0.0, 2.0, ("present", "source"), 3, 4, 1.5, True, 2.0),
            Chunk(2, 4, "not_present", 2, 4.0, 1, False, 2),
            Chunk(4.0, 5.0, None, 0, None, None, False, 0.0),
        ),
    ),
    ("quality", (b"\x00\xff", ({0: array("d", [0.0, 1.25])}, {}), ((0.0, 2.0),))),
    ("quality", (b"", (), ())),
]


def exact(actual, expected):
    assert type(actual) is type(expected)
    assert actual == expected
    if isinstance(expected, (tuple, list)):
        for a, b in zip(actual, expected, strict=True):
            exact(a, b)
    if isinstance(expected, dict):
        for key in expected:
            exact(actual[key], expected[key])
    if isinstance(expected, array):
        assert actual.typecode == "d"


@pytest.mark.parametrize("name,value", VALUES)
def test_exact_round_trip(physical, name, value):
    path, identity = physical
    module = cache()
    module.store(path, path.parent, identity, name, (0.125, ("label",)), value)
    found, loaded = module.load(path, identity, name, (0.125, ("label",)))
    assert found
    exact(loaded, value)
    assert module.load(path, identity, name, (0.126,))[0] is False


@pytest.mark.parametrize(
    "name,value",
    [
        ("rows", True),
        ("rows", -1),
        ("rows", 1.0),
        ("other", 1),
        ("chunks", []),
        ("chunks", (Chunk(0, 1, "present", 1, float("nan"), 0, True, 1),)),
        ("quality", (b"x", ({True: array("d", [1])},), ())),
        ("quality", (b"x", ({0: array("f", [1])},), ())),
        ("quality", (b"x", ({0: array("d", [float("inf")])},), ())),
    ],
)
def test_invalid_values_are_not_persisted(physical, name, value):
    path, identity = physical
    cache().store(path, path.parent, identity, name, (), value)
    assert not path.with_suffix(".res").exists()


def document(path):
    return json.loads(zlib.decompress(path.with_suffix(".res").read_bytes()))


@pytest.mark.parametrize(
    "damage",
    [
        "corrupt",
        "truncated",
        "magic",
        "producer",
        "identity",
        "entries",
        "value",
        "trailing",
        "compressed",
        "decoded",
    ],
)
def test_damaged_files_miss_and_can_be_replaced(physical, damage):
    path, identity = physical
    module = cache()
    module.store(path, path.parent, identity, "rows", (), 7)
    sidecar = path.with_suffix(".res")
    data = document(path)
    if damage in ("magic", "producer", "identity", "entries"):
        data[damage] = "wrong"
    if damage == "value":
        data["entries"][next(iter(data["entries"]))] = True
    raw = zlib.compress(json.dumps(data).encode())
    if damage == "corrupt":
        raw = b"garbage"
    if damage == "truncated":
        raw = raw[:8]
    if damage == "trailing":
        raw += b"extra"
    sidecar.write_bytes(raw)
    with (
        patch.object(module, "MAX_FILE_BYTES", 1 if damage == "compressed" else 4194304),
        patch.object(module, "MAX_RAW_BYTES", 1 if damage == "decoded" else 16777216),
    ):
        assert module.load(path, identity, "rows", ()) == (False, None)
    module.store(path, path.parent, identity, "rows", (), 8)
    assert module.load(path, identity, "rows", ()) == (True, 8)


def test_stale_identity_producer_and_late(physical):
    path, identity = physical
    module = cache()
    module.store(path, path.parent, identity, "rows", (), 7)
    assert module.load(path, (*identity[:2], ((0, 1),)), "rows", ()) == (False, None)
    with patch.object(module, "_producer", return_value="changed"):
        assert module.load(path, identity, "rows", ()) == (False, None)
    os.utime(path, ns=(identity[0], identity[0] + 1000000))
    assert module.load(path, identity, "rows", ()) == (False, None)
    module.store(path, path.parent, identity, "rows", (), 9)
    assert len(document(path)["entries"]) == 1
    assert module.load(path, (*summary_cache.stamp(path), ()), "rows", ()) == (False, None)


def test_limits_merge_and_eviction(physical):
    path, identity = physical
    module = cache()
    for i in range(257):
        module.store(path, path.parent, identity, "rows", (i,), i)
    assert len(document(path)["entries"]) == 256
    assert module.load(path, identity, "rows", (0,)) == (False, None)
    assert module.load(path, identity, "rows", (256,)) == (True, 256)
    with patch.object(module, "MAX_VALUE_BYTES", 1):
        module.store(path, path.parent, identity, "rows", (999,), 999)
    assert module.load(path, identity, "rows", (999,)) == (False, None)


def test_loop_refuses_disk_access(physical):
    path, identity = physical

    async def run():
        with pytest.raises(RuntimeError, match="executor"):
            cache().load(path, identity, "rows", ())
        with pytest.raises(RuntimeError, match="executor"):
            cache().store(path, path.parent, identity, "rows", (), 1)

    asyncio.run(run())


def test_failed_write_and_identity_race_leave_no_temporary(physical):
    path, identity = physical
    module = cache()
    with patch.object(module.os, "replace", side_effect=OSError("disk full")):
        module.store(path, path.parent, identity, "rows", (), 1)
    assert not path.with_suffix(".res").exists()
    assert not list(path.parent.glob("*.restmp"))
    # The day file changes after the merge was checked and before the replace.
    with patch.object(module, "_current", side_effect=[True, False]):
        module.store(path, path.parent, identity, "rows", (), 1)
    assert not path.with_suffix(".res").exists()
    assert not list(path.parent.glob("*.restmp"))


def test_shared_budget_and_invalidation(physical):
    path, _ = physical
    result = path.with_suffix(".res")
    result.write_bytes(b"results")
    summary = path.with_suffix(".sum")
    summary.write_bytes(b"index")
    temporary = path.with_suffix(".restmp")
    temporary.write_bytes(b"interrupted")
    assert summary_cache.disk_bytes(path.parent) == 12
    summary_cache.prune(path.parent, max_bytes=0)
    assert not result.exists() and not summary.exists() and not temporary.exists()
    result.write_bytes(b"results")
    summary_cache.invalidate(path)
    assert not result.exists()


def test_the_process_keeps_only_its_newest_parsed_files(tmp_path):
    module = cache()
    module._files.clear()
    days = []
    for index in range(3):
        path = tmp_path / f"{index}.day"
        path.write_bytes(b"synthetic day %d" % index)
        days.append((path, (*summary_cache.stamp(path), ())))
    with patch.object(module, "MEMORY", 2):
        for path, identity in days:
            module.store(path, tmp_path, identity, "rows", (), 5)
        assert [key.name for key in module._files] == ["1.res", "2.res"]
        for path, identity in days:  # The forgotten one is read from disk again.
            assert module.load(path, identity, "rows", ()) == (True, 5)


def test_a_days_results_are_read_from_disk_once_per_process(physical):
    """Regression (Learn speed): every lookup decompressed and parsed the whole file again."""
    path, identity = physical
    module = cache()
    for index in range(3):
        module.store(path, path.parent, identity, "rows", (index,), index)
    module._files.clear()  # As in a new worker process.
    with patch.object(module, "_read", wraps=module._read) as read:
        for index in range(3):
            assert module.load(path, identity, "rows", (index,)) == (True, index)
            module.store(path, path.parent, identity, "rows", (index, "more"), index)
    assert read.call_count == 1


def test_writes_prune_the_sidecars_now_and_then_not_every_time(physical):
    """Regression (Learn speed): each write walked the whole history folder to prune it."""
    path, identity = physical
    module = cache()
    with patch.object(summary_cache, "prune") as prune:
        for index in range(130):
            module.store(path, path.parent, identity, "rows", (index,), index)
    assert 1 <= prune.call_count <= 3


def test_full_physical_indexes_never_evict_results(tmp_path):
    """Regression: on real history the large ``.sum`` indexes filled the shared budget, so
    every prune deleted the small ``.res`` results and no Learn ever found one."""
    module = cache()
    folder = tmp_path / "radar"
    folder.mkdir()
    for index in range(4):
        (folder / f"{index}.sum").write_bytes(b"i" * 100)
        (folder / f"{index}.res").write_bytes(b"r" * 10)
        stamp = 1_000_000_000 + index
        os.utime(folder / f"{index}.res", ns=(stamp, stamp))  # Older than every index.
    with patch.object(module, "MAX_DISK_BYTES", 25):
        summary_cache.prune(tmp_path)
    assert list(folder.glob("*.sum")) == []  # Legacy indexes are removed,
    assert sorted(path.name for path in folder.glob("*.res")) == [
        "2.res",
        "3.res",
    ]  # results to theirs.


def worker_days(path, persistent=True, late=()):
    from tuner_under_test.worker import calculations, capture, history

    request = {
        "days": [
            {
                "device_id": "radar",
                "date": "1970-01-01",
                "path": str(path),
                "identity": history._identity(path.stat()),
                "late": list(late),
                "persistent": persistent,
            }
        ],
        "budgets": vars(capture.CaptureLimits()),
    }
    return calculations.ReadOnlyDays(request)


def test_worker_identity_tracks_persistence_and_changes(physical):
    import datetime

    path, identity = physical
    date = datetime.date(1970, 1, 1)
    store = worker_days(path)
    assert store.result_identity("radar", date) == (path, identity)
    assert worker_days(path, persistent=False).result_identity("radar", date) is None
    assert store.result_identity("missing", date) is None
    os.utime(path, ns=(identity[0], identity[0] + 1000000))
    assert store.result_identity("radar", date) is None
    path.unlink()
    assert store.result_identity("radar", date) is None


@pytest.mark.parametrize("persistent", [True, False])
def test_remembered_survives_fresh_worker_only_for_persistent_days(physical, persistent):
    import datetime

    from tuner_under_test.history import day_results

    path, _ = physical
    date = datetime.date(1970, 1, 1)
    calls = []

    def compute():
        calls.append(1)
        return 7

    for _ in range(2):
        day_results.clear()
        assert (
            day_results.remembered(
                worker_days(path, persistent), "radar", date, "rows", (), compute
            )
            == 7
        )
    assert len(calls) == (1 if persistent else 2)
    assert path.with_suffix(".res").exists() is persistent


def test_remembered_rechecks_identity_after_disk_load(physical):
    import datetime

    from tuner_under_test.history import day_results

    path, identity = physical
    date = datetime.date(1970, 1, 1)
    module = cache()
    module.store(path, path.parent, identity, "rows", (), 7)
    store = worker_days(path)
    original = module.load

    def changed(*args):
        value = original(*args)
        os.utime(path, ns=(identity[0], identity[0] + 1000000))
        return value

    day_results.clear()
    with patch.object(module, "load", side_effect=changed) as loaded:
        assert day_results.remembered(store, "radar", date, "rows", (), lambda: 8) == 8
        assert loaded.call_count == 1


def test_worker_learn_twice_reuses_chunks(tmp_path):
    from types import SimpleNamespace

    from tuner_under_test.calibration import event_fitting, evidence_plan
    from tuner_under_test.history import day_results, dayfile, daystore
    from tuner_under_test.worker import calculations, capture

    store = daystore.DayStore(tmp_path / "history")
    date = daystore.date_of(100)
    builder = dayfile.DayBuilder(*daystore.bounds(date))
    for i in range(240):
        builder.add(dayfile.Change(i * 5.0, {0: 40 + i % 2 if i < 120 else i % 2}))
    builder.add(dayfile.End(1200))
    store.replace("radar", date, builder.day())
    labels = [
        {"start": 0, "end": 600, "state": "present"},
        {"start": 600, "end": 1200, "state": "not_present"},
    ]
    source = SimpleNamespace(
        data={"devices": {"radar": {"history_labels": labels}}}, _days=store, _history_until=1200
    )
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor() as executor:
        request = executor.submit(capture.prepare, source, "radar", tmp_path / "private").result()
    request.update(
        operation="learn",
        entities={"g0_move": "number.gate"},
        current={"g0_move": 20},
        timing={"timeout": 0},
        constraints={"g0_move": {"min": 20, "max": 20, "step": 1}},
    )
    with (
        patch.object(event_fitting, "LEARNING_ROWS", 50),
        patch.object(calculations.time, "time", return_value=1200.0),
    ):
        day_results.clear()
        with patch.object(evidence_plan, "_day_chunks", wraps=evidence_plan._day_chunks) as chunks:
            first = calculations.execute(request)
            assert chunks.call_count > 0
        assert list(store.root.rglob("*.res"))
        day_results.clear()
        with patch.object(evidence_plan, "_day_chunks", side_effect=AssertionError("recomputed")):
            second = calculations.execute(request)
    assert second == first


@pytest.mark.parametrize("damage", ["identity_type", "key", "extra", "duplicate"])
def test_strict_envelope(physical, damage):
    path, identity = physical
    module = cache()
    module.store(path, path.parent, identity, "rows", (), 7)
    data = document(path)
    if damage == "identity_type":
        data["identity"][1] = float(identity[1])
    if damage == "key":
        data["entries"]["x" * 32] = 1
    if damage == "extra":
        data["unexpected"] = True
    raw = json.dumps(data)
    if damage == "duplicate":
        raw = raw.replace('{"magic":', '{"magic": "wrong", "magic":', 1)
    path.with_suffix(".res").write_bytes(zlib.compress(raw.encode()))
    assert module.load(path, identity, "rows", ()) == (False, None)


@pytest.mark.parametrize("limit", ["MAX_FILE_BYTES", "MAX_RAW_BYTES"])
def test_write_limits_do_not_publish(physical, limit):
    path, identity = physical
    module = cache()
    with patch.object(module, limit, 1):
        module.store(path, path.parent, identity, "rows", (), 7)
    assert not path.with_suffix(".res").exists()
    assert not list(path.parent.glob("*.restmp"))


@pytest.mark.parametrize(
    "bad",
    [
        ["unknown", []],
        ["tuple"],
        ["array", [True]],
        ["array", [float("inf")]],
        ["bytes", 4],
        ["bytes", "zz"],
        ["tuple", "bad"],
        ["chunk", []],
        ["dict", [[0, 1], [0, 2]]],
        {"bad": 1},
    ],
)
def test_invalid_tagged_values_are_misses(physical, bad):
    path, identity = physical
    module = cache()
    module.store(path, path.parent, identity, "rows", (), 7)
    data = document(path)
    data["entries"][next(iter(data["entries"]))] = bad
    path.with_suffix(".res").write_bytes(zlib.compress(json.dumps(data).encode()))
    assert module.load(path, identity, "rows", ()) == (False, None)

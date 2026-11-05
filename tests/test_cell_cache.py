"""Finished physical digest caching uses synthetic files, never private history."""

from datetime import date
from unittest.mock import patch

import pytest
import test_tuner  # noqa: F401
from test_hybrid_rows import source
from tuner_under_test.history import cell_cache, summary_cache
from tuner_under_test.history.fit_days import FittingDays

DAY = date(1970, 1, 1)


def fitting(tmp_path):
    rows, days, path = source(tmp_path)
    return FittingDays(rows.view), days, path


def test_finished_day_cache_survives_worker_restart(tmp_path):
    fit, days, path = fitting(tmp_path)
    a = fit.digest("synthetic", DAY, (0,))
    assert path.with_suffix(".cell").read_bytes().startswith(b"LD24CELL1")
    with patch.object(days, "open_columns", side_effect=AssertionError("decoded warm rows")):
        b = FittingDays(fit.view).digest("synthetic", DAY, (0,))
    assert a.channels == b.channels
    assert list(a.segments()) == list(b.segments())


def test_physical_rows_reuse_sidecar_without_decoding_energy_index(tmp_path, monkeypatch):
    fit, days, path = fitting(tmp_path)
    original = fit.digest("synthetic", DAY, (0,))
    expected = original.physical.window(0, 86400)

    def forbidden(*_args):
        raise AssertionError("physical replay decoded the energy index")

    monkeypatch.setattr(cell_cache, "read_index", forbidden)
    with patch.object(days, "open_columns", side_effect=AssertionError("decoded warm rows")):
        actual = FittingDays(fit.view).physical("synthetic", DAY, (0,))
    assert actual.window(0, 86400) == expected
    assert path.with_suffix(".cell").exists()


def test_physical_replay_rebuilds_corrupt_sidecar(tmp_path):
    fit, days, path = fitting(tmp_path)
    expected = fit.digest("synthetic", DAY, (0,)).physical.window(0, 86400)
    path.with_suffix(".cell").write_bytes(b"corrupt")
    with patch.object(days, "open_columns", wraps=days.open_columns) as reads:
        actual = FittingDays(fit.view).physical("synthetic", DAY, (0,))
    assert actual.window(0, 86400) == expected
    assert reads.call_count == 1
    assert path.with_suffix(".cell").read_bytes().startswith(b"LD24CELL1")


def test_live_day_is_folded_on_every_request(tmp_path):
    fit, days, path = fitting(tmp_path)
    days.entries[("synthetic", DAY)]["persistent"] = False
    original = days.open_columns
    with patch.object(days, "open_columns", wraps=original) as reads:
        fit.digest("synthetic", DAY, (0,))
        fit.digest("synthetic", DAY, (0,))
    assert reads.call_count == 2
    assert not path.with_suffix(".cell").exists()


def test_labels_do_not_invalidate_physical_digest(tmp_path):
    fit, days, _ = fitting(tmp_path)
    a = fit.digest("synthetic", DAY, (0,))
    fit.view.data = {"history_labels": [{"state": "present"}]}
    with patch.object(days, "open_columns", side_effect=AssertionError("decoded on label edit")):
        b = fit.digest("synthetic", DAY, (0,))
    assert a.channels == b.channels


def test_late_guess_metadata_is_neither_cached_nor_an_invalidation_key(tmp_path):
    fit, days, path = fitting(tmp_path)
    entry = days.entries[("synthetic", DAY)]
    entry["late"] = [(12, 1, 2, "private-provenance-marker", 2, 0)]
    fit.digest("synthetic", DAY, (0,))
    assert b"private-provenance-marker" not in path.with_suffix(".cell").read_bytes()
    entry["late"] = []
    with patch.object(days, "open_columns", side_effect=AssertionError("decoded for guess edit")):
        assert fit.digest("synthetic", DAY, (0,)) is not None


def test_day_rewrite_selected_channels_and_cache_version_invalidate(tmp_path, monkeypatch):
    fit, days, path = fitting(tmp_path)
    fit.digest("synthetic", DAY, (0,))
    identity = days.result_identity("synthetic", DAY)[1]
    assert cell_cache.load(path, identity, (0, 1)) is None
    monkeypatch.setattr(cell_cache, "CELL_CACHE_VERSION", 99)
    assert cell_cache.load(path, identity, (0,)) is None
    monkeypatch.undo()
    path.write_bytes(path.read_bytes() + b"x")
    assert cell_cache.load(path, identity, (0,)) is None


@pytest.mark.parametrize("damage", ["corrupt", "truncated", "oversized"])
def test_corrupt_truncated_oversized_cell_is_rebuilt(tmp_path, damage, monkeypatch):
    fit, _, path = fitting(tmp_path)
    a = fit.digest("synthetic", DAY, (0,))
    cache = path.with_suffix(".cell")
    if damage == "corrupt":
        cache.write_bytes(b"broken")
    elif damage == "truncated":
        cache.write_bytes(cache.read_bytes()[:-1])
    else:
        monkeypatch.setattr(cell_cache, "CELL_CACHE_DISK_BYTES", 100)
    b = fit.digest("synthetic", DAY, (0,))
    assert a.channels == b.channels


def test_failed_write_still_returns_computed_digest(tmp_path):
    fit, _, path = fitting(tmp_path)
    with patch("os.replace", side_effect=OSError("disk full")):
        assert fit.digest("synthetic", DAY, (0,)).cov[0] == 2
    assert not path.with_suffix(".cell").exists()
    assert not list(tmp_path.rglob("*.celltmp"))


def test_trim_clear_and_disk_accounting_include_cells(tmp_path):
    fit, _, path = fitting(tmp_path)
    fit.digest("synthetic", DAY, (0,))
    cache = path.with_suffix(".cell")
    assert summary_cache.disk_bytes(tmp_path) == cache.stat().st_size
    orphan = tmp_path / "orphan.celltmp"
    orphan.write_bytes(b"partial")
    summary_cache.prune(tmp_path)
    assert not orphan.exists()
    assert cache.exists()
    summary_cache.invalidate(path)
    assert not cache.exists()


def test_cell_budget_is_independent_of_result_budget(tmp_path):
    fit, _, path = fitting(tmp_path)
    fit.digest("synthetic", DAY, (0,))
    result = path.with_suffix(".res")
    result.write_bytes(b"result")
    summary_cache.prune(tmp_path, max_bytes=0)
    assert not result.exists()
    assert path.with_suffix(".cell").exists()


def test_worker_trim_removes_cells_before_considering_physical_history(tmp_path):
    from tuner_under_test.history.daystore import DayStore
    from tuner_under_test.worker import maintenance

    store = DayStore(tmp_path)
    cache = tmp_path / "orphan.cell"
    cache.write_bytes(b"auxiliary" * 100)
    _, pruned = maintenance._prune(store, {"devices": {}}, 1, tmp_path)
    assert pruned
    assert not cache.exists()


def test_rewrite_and_delete_remove_cells_from_real_store(tmp_path):
    from test_day_summaries import synthetic_day
    from tuner_under_test.history.daystore import DayStore, date_of

    store = DayStore(tmp_path / "history")
    day = synthetic_day()
    date = date_of(day.start)
    store.replace("synthetic", date, day)
    path = store._path("synthetic", date, ".cell")
    path.write_bytes(b"old cache")
    store.replace("synthetic", date, day)
    assert not path.exists()
    path.write_bytes(b"old cache")
    store.delete("synthetic", [date])
    assert not path.exists()


def test_partial_geometry_roundtrip_is_streamed(tmp_path):
    from test_hybrid_digest import block
    from tuner_under_test.engine.hybrid.digest import fold_day

    path = tmp_path / "source.day"
    path.write_bytes(b"synthetic")
    identity = summary_cache.stamp(path)
    rows = [(i / 1000, (i + 0.5) / 1000, [40] * 18) for i in range(5000)]
    digest = fold_day([block(rows)], 0, 6, (0,))
    cell_cache.store(path, tmp_path, identity, (0,), digest)
    loaded = cell_cache.load(path, identity, (0,))
    assert list(loaded.segments()) == list(digest.segments())
    assert loaded.partial._file._rolled

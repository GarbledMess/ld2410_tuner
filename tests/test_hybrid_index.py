"""Label-independent energy positions survive the bounded binary sidecar."""

import random

import pytest
import test_tuner  # noqa: F401
from test_cell_cache import DAY, fitting
from tuner_under_test.engine.hybrid.index import EnergyIndex
from tuner_under_test.history import cell_cache


def test_index_bands_equal_every_strict_scalar_crossing():
    rng = random.Random(198)
    columns = tuple(
        bytes(rng.choice((0, 20, 40, 41, 100, 255)) for _ in range(4096)) for _ in range(18)
    )
    index = EnergyIndex.build(columns)
    for c in range(18):
        for threshold in (0, 20, 40, 41, 99, 100):
            got = index.mask(c, threshold, 17, 3001)
            assert got == sum(
                1 << i for i, v in enumerate(columns[c][17:3001]) if threshold < v <= 100
            )
    assert index.nbytes() < 2 * 1024**2
    assert index.bounds[0] == (0, 100)


def test_warm_cell_sidecar_loads_index_without_rebuilding(tmp_path, monkeypatch):
    fit, days, path = fitting(tmp_path)
    original = fit.digest("synthetic", DAY, (0,))
    expected = EnergyIndex.build(original.channels)

    def forbidden(_):
        raise AssertionError("rebuilt warm index")

    monkeypatch.setattr(EnergyIndex, "build", forbidden)
    cached = cell_cache.load(path, days.result_identity("synthetic", DAY)[1], (0,))
    assert cached.index.bounds == expected.bounds
    assert cached.index.mask(0, 40) == expected.mask(0, 40)


@pytest.mark.parametrize("lo,hi", [(0, 0), (1, 9), (2, 11)])
def test_index_ranges_are_clipped_without_leaking_positions(lo, hi):
    index = EnergyIndex.build((bytes([0, 20, 41, 100, 255] * 3),))
    assert index.band(0, 20, 41, lo, hi) == sum(1 << (i - lo) for i in range(lo, hi) if i % 5 == 2)

"""Warm replay work counts use synthetic inputs only."""

from dataclasses import replace

import pytest
import test_tuner  # noqa: F401
from test_hybrid_reports import problem
from tuner_under_test.engine.hybrid import exact, fixed, replay


@pytest.mark.parametrize("timing", [{}, {"timeout": 0, "on_delay": 0, "off_delay": 0}])
def test_zero_timing_raw_report_reuses_physical_replay(monkeypatch, timing):
    p = problem()
    p = replace(p, radars={"r": replace(p.radars["r"], timing=timing)})
    pattern = {"r": {0: 50}}
    calls = []
    original = exact.replay

    def counted(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(exact, "replay", counted)
    with fixed.FixedScores(p, 1) as scores:
        timed = scores.scores([pattern], p.zones)
        raw = scores.scores([pattern], p.zones, raw=True)
    assert raw == timed
    assert len(calls) == 1


def test_support_tables_compile_with_byte_translations(monkeypatch):
    from types import SimpleNamespace

    from tuner_under_test.const import HISTORY_KEYS

    class Model:
        def supports(self, key, energy):
            return energy % 3 == HISTORY_KEYS.index(key) % 3

    radar = SimpleNamespace(channels=(0, 17))
    model = Model()
    expected = {
        c: tuple(
            bytes(t < v <= 100 and model.supports(HISTORY_KEYS[c], v) for v in range(256))
            for t in range(101)
        )
        for c in radar.channels
    }

    def forbidden(*_args):
        raise AssertionError("support table merged one scalar energy per threshold")

    monkeypatch.setattr(replay, "min", forbidden, raising=False)
    assert replay.support_tables(radar, model) == expected
    assert replay.support_tables(radar, None) is None


def test_physical_replay_spool_compresses_bounded_blocks(tmp_path, monkeypatch):
    from array import array
    from types import SimpleNamespace

    times = array("d", range(8192))
    ends = array("d", range(1, 8193))
    expected = times, ends, {0: b"\x28\x29" * 4096}
    source = SimpleNamespace(window=lambda _lo, _hi: expected)

    def forbidden(*_args):
        raise AssertionError("compressible replay rows exceeded the memory prefix")

    monkeypatch.setattr(exact._RowSpool, "_open", forbidden)
    provider = exact._Physical(source, (0,), tmp_path, 65536)
    try:
        assert provider.window(0, 8192) == expected
        provider.rewind()
        assert provider.window(0, 8192) == expected
        assert provider.size == 0
    finally:
        provider.close()

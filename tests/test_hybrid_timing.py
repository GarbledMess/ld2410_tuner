"""Streaming timing parity on synthetic physical and pooled intervals."""

import random
from types import SimpleNamespace

import pytest
import test_tuner  # noqa: F401
from hybrid_referee import replay, union
from tuner_under_test.engine.hybrid.timing import TimingStream


def timed(segments, hits, timing, cuts=()):
    stream = TimingStream(timing)
    cov, act = [], []
    edges = sorted({x for pair in segments for x in pair} | set(cuts))
    for a, b in zip(edges, edges[1:], strict=False):
        covered = [(max(a, x), min(b, y)) for x, y in segments if x < b and y > a]
        stream.feed(covered, ((max(a, x), min(b, y)) for x, y in hits if x < b and y > a))
        c, d = stream.drain(stream.frontier())
        cov.extend(c)
        act.extend(d)
    stream.finish()
    c, d = stream.drain(stream.frontier())
    return union(cov + c), union(act + d)


def oracle(segments, hits, timing):
    edges = sorted({x for pair in segments + hits for x in pair})
    rows = [
        (a, b)
        for a, b in zip(edges, edges[1:], strict=False)
        if any(x <= a < y for x, y in segments)
    ]
    radar = SimpleNamespace(
        times=[a for a, b in rows],
        ends=[b for a, b in rows],
        columns=[bytes(90 if any(x <= a < y for x, y in hits) else 0 for a, b in rows)],
        timing=timing,
    )
    result = replay(radar, {0: 50}, (edges[0], edges[-1]))
    return list(result.coverage), list(result.active)


@pytest.mark.parametrize("resolution", [1, 2, 6])
def test_cell_aligned_pulses_match_referee_exactly(resolution):
    rng = random.Random(723 + resolution)
    for _ in range(60):
        segments = [(0, 180), (186, 360)]
        hits = [
            (t, t + resolution)
            for t in range(0, 360, resolution)
            if rng.random() < 0.3 and not 180 <= t < 186
        ]
        timing = {
            "timeout": rng.choice([0, 2.5, 12]),
            "on_delay": rng.choice([0, 5, 17]),
            "off_delay": rng.choice([0, 3, 15]),
        }
        assert timed(segments, hits, timing, range(0, 360, 13)) == oracle(segments, hits, timing)


def test_subcell_pulses_bounded_error():
    exact = timed([(0, 60)], [(10, 10.5)], {"timeout": 2.5, "on_delay": 0, "off_delay": 3})[1]
    pooled = timed([(0, 60)], [(6, 12)], {"timeout": 2.5, "on_delay": 0, "off_delay": 3})[1]
    assert 0 <= exact[0][0] - pooled[0][0] < 6
    assert 0 <= pooled[0][1] - exact[0][1] < 6


def test_on_delay_pooling_bias_is_documented():
    timing = {"timeout": 0, "on_delay": 5, "off_delay": 0}
    assert timed([(0, 60)], [(10, 10.5)], timing)[1] == []
    assert timed([(0, 60)], [(6, 12)], timing)[1] == [(11, 12)]


def test_premerge_only_joins_within_hold_and_segment():
    segments = [(0, 10), (11, 30)]
    timing = {"timeout": 3, "on_delay": 0, "off_delay": 2}
    hits = [(1, 2), (4, 5), (9, 10), (11, 12), (20, 21)]
    assert timed(segments, hits, timing)[1] == [(1, 10), (11, 17), (20, 26)]


def test_timing_state_survives_midnight_and_block_boundary():
    segments = [(86390, 86440)]
    hits = [(86397, 86403), (86407, 86410)]
    timing = {"timeout": 5, "on_delay": 7, "off_delay": 4}
    assert timed(segments, hits, timing, [86400, 86406, 86409]) == oracle(segments, hits, timing)


def test_repeated_hit_chain_releases_memory():
    stream = TimingStream({"timeout": 3, "on_delay": 5, "off_delay": 2})
    active = []
    for t in range(10000):
        stream.feed([(t, t + 1)], [(t, t + 0.5)])
        assert stream.frontier() == t + 1
        cov, act = stream.drain(t + 1)
        assert cov == [(t, t + 1)]
        active.extend(act)
        assert len(stream.active) <= 1
        assert len(stream.coverage) <= 1
    stream.finish()
    active.extend(stream.drain(stream.frontier())[1])
    assert union(active) == [(5, 10000)]


@pytest.mark.parametrize(
    "timing",
    [{"timeout": -1}, {"timeout": float("nan")}, {"timeout": 1, "on_delay": -1, "off_delay": 0}],
)
def test_invalid_timing_rejected(timing):
    with pytest.raises(ValueError, match="Timing"):
        TimingStream(timing)


def test_unknown_timing_contract():
    assert timed([(0, 20)], [(2, 3)], {"on_delay": 10, "off_delay": 10})[1] == [(2, 3)]
    assert timed([(0, 20)], [(2, 3)], {"timeout": 4, "on_delay": None, "off_delay": 10})[1] == [
        (2, 7)
    ]


def test_frontier_never_advances_over_unprocessed_empty_time():
    stream = TimingStream({})
    assert stream.frontier() == float("-inf")
    stream.feed([(10, 20)], [])
    stream.close()
    assert stream.frontier() == 20
    with pytest.raises(ValueError, match="frontier"):
        stream.drain(21)
    stream.feed([], [])
    assert stream.frontier() == 20
    stream.finish()
    assert stream.frontier() == 20

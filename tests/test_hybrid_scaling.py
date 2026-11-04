"""Synthetic raw-history workload contracts for full-worker scaling acceptance."""

import pytest
import test_tuner  # noqa: F401


def test_scaling_fixture_covers_full_days_and_keeps_a_live_day(tmp_path):
    from hybrid_scaling import DAY, START, build_fixture

    fixture = build_fixture(tmp_path, radars=2, days=2)
    assert START % DAY == 0
    assert fixture["radar_days"] == 4
    assert fixture["base_cadence_seconds"] == 6
    assert fixture["labelled_duty_cycle"] == pytest.approx(1 / 3)
    assert fixture["source_rows"] > 4 * (DAY // 6 - 100)
    store = fixture["store"]
    for rid in ("r0", "r1"):
        dates = store.days(rid)
        assert len(dates) == 2
        assert store.live_date(rid) == dates[-1]
        for date in dates:
            day = store.day(rid, date, until=START + 2 * DAY)
            assert day.times[0] == day.start
            assert day.ends[-1] == day.end
            assert len(day.columns) == 18
            assert all(len(set(column)) > 2 for column in day.columns)
            durations = [b - a for a, b in zip(day.times, day.ends, strict=True)]
            assert 0.125 in durations
            assert sum(durations) == DAY - 60
        labels = fixture["devices"][rid]["history_labels"]
        assert len(labels) >= 8
        assert sum(item["end"] - item["start"] for item in labels) == 2 * DAY / 3


def test_scaling_fixture_is_reproducible_and_density_preserves_coverage(tmp_path):
    from hybrid_scaling import DAY, START, build_fixture

    fixtures = [
        build_fixture(tmp_path / name, radars=1, days=1, density=density)
        for name, density in (("a", 1), ("b", 1), ("dense", 2))
    ]
    days = [
        item["store"].day("r0", item["store"].days("r0")[0], until=START + DAY) for item in fixtures
    ]
    assert days[0] == days[1]
    assert fixtures[0]["devices"] == fixtures[1]["devices"]
    assert len(days[2].times) > len(days[0].times) * 1.9
    coverage = [sum(b - a for a, b in zip(day.times, day.ends, strict=True)) for day in days]
    assert coverage[0] == coverage[2]


def test_scaling_fixture_contains_correlated_and_opposite_response_radars(tmp_path):
    from hybrid_scaling import DAY, START, build_fixture

    fixture = build_fixture(tmp_path, radars=3, days=1)
    vectors = []
    for rid in ("r0", "r1", "r2"):
        store = fixture["store"]
        day = store.day(rid, store.days(rid)[0], until=START + DAY)
        vectors.append([day.vector(day.times.index(START + hour * 3600 + 1800)) for hour in (6, 8)])
    for channel in range(18):
        assert vectors[0][0][channel] > vectors[0][1][channel] + 20
        assert vectors[1] == vectors[0]
        assert vectors[2][0][channel] + 20 < vectors[2][1][channel]


@pytest.mark.parametrize("field", ["radars", "days", "density"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_scaling_fixture_rejects_invalid_dimensions(tmp_path, field, value):
    from hybrid_scaling import build_fixture

    with pytest.raises(ValueError, match=field):
        build_fixture(tmp_path, **{field: value})

"""Today's chart keeps its finished buckets: a resumed chart equals one charted afresh."""

import random
from unittest.mock import patch

import pytest
from test_event_charts import row, view
from tuner_under_test.presentation import charts
from tuner_under_test.worker import display

KEYS = ["g0_move", "g1_move"]


def recorded(seed, end):
    """A radar-day from 0 with value changes, missing readings and recording gaps."""
    rng = random.Random(seed)
    blocks, changes, t = [], [], 0.0
    while t < end:
        if rng.random() < 0.02:
            blocks.append((changes, t))
            changes, t = [], t + rng.choice((7, 60, 400))
            continue
        changes.append((t, row(rng.choice((None, 5, 30, 30, 80)), rng.randrange(100))))
        t += rng.choice((0.125, 1, 6, 6, 30))
    blocks.append((changes, end))
    return view([], end=end, blocks=blocks, pending=True)


def drawn(runtime, end, max_points, resume=None):
    """The day so far, as the panel asks for it: from midnight (0) to ``end``."""
    runtime._history_until = end
    with patch("time.time", return_value=end):
        chart = charts.history_series_multi(
            runtime, "a", KEYS, end / 3600, max_points, end, resume=resume
        )
    return display.drawn(chart)


def resume(chart, end, max_points):
    start, bucket_span, _ = charts.chart_window({}, end / 3600, max_points, end)
    return charts.resume_from(chart, start, end, bucket_span)


@pytest.mark.parametrize("seed", range(6))
@pytest.mark.parametrize("max_points", [50, 400])
def test_a_resumed_day_chart_equals_charting_the_whole_day(seed, max_points):
    runtime = recorded(seed, 30000)
    earlier = drawn(runtime, 21600, max_points)
    for later in (21600, 21601, 22000, 23000):  # Same bucket size at either budget.
        kept = resume(earlier, later, max_points)
        assert kept is not None
        tail = drawn(runtime, later, max_points, kept)
        assert charts.spliced(earlier, tail, kept) == drawn(runtime, later, max_points)


def test_a_new_bucket_size_or_start_charts_afresh():
    runtime = recorded(1, 30000)
    earlier = drawn(runtime, 21600, 400)
    # 21600 s at 400 points takes 120 s buckets; a whole day at 400 points does not.
    assert resume(earlier, 86400, 400) is None
    assert charts.resume_from(earlier, 60.0, 21700, earlier["bucket_seconds"]) is None
    assert resume(earlier, 21000, 400) is None  # Never backwards.
    assert resume(drawn(runtime, 200, 400), 260, 400) is None  # Too new to have settled.


@pytest.mark.parametrize(
    "resume",
    [
        {"from": 21540.5, "points": {}},
        {"from": -120, "points": {}},
        {"from": 21540, "points": {"g0_move": -1}},
        {"from": 21540, "points": {"g8_still": 2}},
        {"from": 21540, "points": {"g0_move": 2.5}},
    ],
)
def test_a_resume_outside_the_chart_is_refused(resume):
    with pytest.raises(ValueError):
        drawn(recorded(1, 30000), 21600, 400, resume)


def served(monkeypatch):
    """A radar whose display jobs run in process, recording each job's request."""
    import asyncio
    from collections import OrderedDict
    from types import SimpleNamespace

    from tuner_under_test.worker import bridge

    source = recorded(3, 30000)
    source._history_cache, source._history_jobs = OrderedDict(), {}
    source._history_view = lambda *args, **kwargs: source
    source.hass = SimpleNamespace(async_add_executor_job=asyncio.to_thread)
    jobs = []

    async def calculate(runtime, operation, prepare):
        prepared = prepare()
        jobs.append((prepared["parameters"], prepared["views"]["a"]._snapshot_history_start))
        if "device_ids" in prepared["parameters"]:
            return display.calculate_charts(prepared["views"], prepared["parameters"])
        return display.calculate(operation, source, prepared["parameters"])

    monkeypatch.setattr(bridge, "calculate", calculate)
    return source, jobs


def test_a_refreshed_day_chart_charts_only_its_unsettled_buckets(monkeypatch):
    """Regression: each refresh of today's graph charted the whole day again."""
    import asyncio

    source, jobs = served(monkeypatch)

    async def run():
        first = await charts.async_history_series(source, "a", KEYS, 6, 400, 21600)
        later = await charts.async_history_series(source, "a", KEYS, 23000 / 3600, 400, 23000)
        return first, later

    first, later = asyncio.run(run())
    assert "resume" not in jobs[0][0] and jobs[0][1] == 0
    cut = (21600 - charts.SETTLED_SECONDS) // 120 * 120
    assert jobs[1][0]["resume"]["from"] == cut
    assert jobs[1][1] == cut  # Only the history from the cut is copied for the worker.
    assert later == drawn(source, 23000, 400)
    assert first == drawn(source, 21600, 400)


def test_rewritten_history_charts_the_day_afresh(monkeypatch):
    import asyncio

    source, jobs = served(monkeypatch)

    async def run():
        await charts.async_history_series(source, "a", KEYS, 6, 400, 21600)
        source._days._rewrite("a")  # Thinning, Trim or Clear changed what was charted.
        return await charts.async_history_series(source, "a", KEYS, 23000 / 3600, 400, 23000)

    later = asyncio.run(run())
    assert "resume" not in jobs[1][0]
    assert later == drawn(source, 23000, 400)


def test_a_refreshed_room_chart_resumes_each_radar(monkeypatch):
    import asyncio

    source, jobs = served(monkeypatch)

    async def run():
        await charts.async_room_series(source, ["a"], KEYS, 6, 400, 21600)
        return await charts.async_room_series(source, ["a"], KEYS, 23000 / 3600, 400, 23000)

    later = asyncio.run(run())
    assert set(jobs[1][0]["resume"]) == {"a"}
    assert later == {"charts": {"a": drawn(source, 23000, 400)}, "errors": {}}

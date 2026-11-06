"""Learn and Apply on the real-shaped home: the job a user starts, through the worker protocol,
judged by what it recommends and what ends up on the radar."""

import asyncio
import time

import pytest
import real_shape
from real_home import WebsocketError
from real_shape import DAY, TODAY

# A fit of these radars costs about a third of this (in units of ``_speed``). One far beyond it
# is the engine getting slower, whatever the suite's other assertions say.
LEARN_BUDGET_UNITS = 80


def _speed():
    """CPU seconds of a fixed Python and zlib workload: this machine's unit of work, so a budget
    means the same on a slow CI runner as on a fast desktop."""
    import zlib

    payload = bytes(range(256)) * 400
    started = time.process_time()
    total = 0
    for index in range(1_000_000):
        total += index % 7
    for _ in range(200):
        zlib.compress(payload, 6)
    return time.process_time() - started


def _reach(radar):
    """The channels this radar sees a person on."""
    return [
        name
        for name in real_shape.channels(radar)
        if int(name[1]) in real_shape._radar_gates(radar)
    ]


def _assert_separates(radar, proposals):
    for name in _reach(radar):
        threshold = proposals[name]["threshold"]
        # Above the room's empty noise (at most 13) on the gates that reach the person, and at
        # or below what an occupied room reads there (from 35 standing still, 50 moving).
        assert 14 <= threshold <= (35 + 13 if name.endswith("still") else 50 + 15), (
            name,
            threshold,
        )


def test_learn_one_radar_then_apply_writes_what_it_recommended(play):
    async def scenario(home):
        radar = "hall_1"
        unit = _speed()
        started = time.process_time()
        job, report = await home.learn(radar)
        cost = (time.process_time() - started) / unit
        assert job["status"] == "running"
        assert report["status"] == "completed", report.get("error")
        assert cost < LEARN_BUDGET_UNITS, f"Learn took {cost:.0f} work units"

        snapshot = await home.ws("snapshot")
        assert snapshot["devices"][radar]["learning_job"]["status"] == "completed"
        saved = home.runtime.data["devices"][radar]["learning_results"]["user"]
        # "ok" or "unsafe": readings arrive late at a room's edges, near the 99.9% target.
        assert saved["status"] in ("ok", "unsafe")
        proposals = saved["proposals"]
        assert set(proposals) == set(real_shape.channels(radar))
        _assert_separates(radar, proposals)
        measured = saved["training"]["duration"]
        assert measured["presence_recall"] >= 0.99
        assert measured["missed_presence_episodes"] == 0
        assert measured["false_trigger_events"] <= 2

        before = home.thresholds(radar)
        assert set(before.values()) == {50.0}
        shown = (await home.ws("snapshot"))["devices"][radar]["current_thresholds"]
        applied = await home.ws(
            "apply", device_id=radar, slot="user", result_id=saved["id"], expected=shown
        )
        assert applied["skipped"] == {}
        expected = {name: float(value["threshold"]) for name, value in proposals.items()}
        assert home.thresholds(radar) == expected
        writes = {
            call[2]["entity_id"]: call[2]["value"]
            for call in home.hass.writes
            if call[:2] == ("number", "set_value")
        }
        assert writes[f"number.{radar}_g1_move_threshold"] == proposals["g1_move"]["threshold"]

        # What was learned no longer applies once its evidence is relabelled.
        await home.ws(
            "label_history",
            device_id=radar,
            start=TODAY - DAY + 100.5,
            end=TODAY - DAY + 400.5,
            state="present",
        )
        with pytest.raises(WebsocketError, match="Learn thresholds"):
            await home.ws("apply", device_id=radar)

    play(scenario)


def test_learn_a_two_radar_room_respects_each_radars_range_and_apply_writes_both(play):
    async def scenario(home):
        unit = _speed()
        started = time.process_time()
        job, report = await home.learn("kitchen_1")
        cost = (time.process_time() - started) / unit
        assert report["status"] == "completed", report.get("error")
        assert report["group_ids"] == ["area:kitchen"]
        assert cost < 4 * LEARN_BUDGET_UNITS, f"Room Learn took {cost:.0f} work units"

        saved = home.runtime.data["room_learning"]["area:kitchen"]["slots"]["user"]
        assert saved["status"] == "ok"
        domain = real_shape.COARSE_DOMAIN
        for threshold in saved["thresholds"][real_shape.COARSE].values():
            assert domain["min"] <= threshold <= domain["max"]
            assert (threshold - domain["min"]) % domain["step"] == 0
        _assert_separates(
            "kitchen_1",
            {
                name: {"threshold": value}
                for name, value in saved["thresholds"]["kitchen_1"].items()
            },
        )

        applied = await home.ws(
            "apply_set", set_id="area:kitchen", result_id=saved["id"], source="user"
        )
        assert applied["skipped"] == {}
        assert home.runtime.data["room_learning"]["area:kitchen"]["application"]["status"] == (
            "applied"
        )
        for radar in ("kitchen_1", real_shape.COARSE):
            assert home.thresholds(radar) == {
                name: float(value) for name, value in saved["thresholds"][radar].items()
            }

    play(scenario)


def test_a_label_edit_and_a_save_during_a_long_fit_land_and_the_stale_fit_is_refused(play):
    async def scenario(home):
        radar = "hall_1"
        in_fit, may_finish = asyncio.Event(), asyncio.Event()

        async def long_fit(request):
            if request.get("operation") == "learn":
                in_fit.set()
                await may_finish.wait()

        home.transport.before = long_fit
        await home.ws("start_learning", device_id=radar)
        await asyncio.wait_for(in_fit.wait(), 60)

        # A fit takes minutes on a small machine. Labelling does not wait for it.
        low, high = TODAY - DAY + 500.5, TODAY - DAY + 800.5
        await asyncio.wait_for(
            home.ws("label_history", device_id=radar, start=low, end=high, state="present"), 20
        )
        # A save queues behind the running job; it neither overtakes the fit nor is lost.
        saving = asyncio.create_task(home.runtime.async_save())
        await asyncio.sleep(0.2)
        assert not saving.done()
        may_finish.set()
        await asyncio.wait_for(saving, 60)
        await asyncio.gather(*home.runtime._learning_jobs.values(), return_exceptions=True)

        report = home.runtime.data["devices"][radar]["learning_job"]
        assert report["status"] == "error"
        assert "labels changed" in report["error"].lower()
        assert not home.runtime.data["devices"][radar].get("learning_results", {}).get("user")
        stored = await home.runtime.store.async_load()
        assert any(x["start"] == low for x in stored["devices"][radar]["history_labels"])

    play(scenario)

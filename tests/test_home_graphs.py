"""Drawing graphs and reading raw days on the real-shaped home, as the panel asks for them."""

import asyncio
import datetime

import real_shape
from real_shape import DAY, TODAY
from tuner_under_test.worker.supervisor import WorkerFailed


def _date(ago):
    return datetime.datetime.fromtimestamp(TODAY - ago * DAY, datetime.UTC).date().isoformat()


def test_a_radar_graph_of_a_past_day_shows_that_days_readings_and_labels(play):
    async def scenario(home):
        ago, radar, room = 3, "office_1", "office"
        start = TODAY - ago * DAY
        graph = await home.ws(
            "history_series_multi",
            device_id=radar,
            keys=["g1_move", "g6_move"],
            hours=24,
            end=start + DAY,
        )
        assert (graph["start"], graph["end"]) == (start, start + DAY)
        busy = real_shape.occupied(room, int(start // DAY))
        reach = graph["series"]["g1_move"]["points"]
        quiet = graph["series"]["g6_move"]["points"]
        assert len(reach) >= 100
        # Gate 1 reaches the person; gate 6 never rises above the room's noise.
        assert max(point["max"] for point in quiet) <= 13
        low, high = busy[0]
        inside = [p for p in reach if start + low + 700 < p["t"] < start + high - 700]
        assert inside and min(p["min"] for p in inside) >= 50
        before = [p for p in reach if p["t"] < start + low - 700 and p["t"] > start + 7 * 3600]
        assert all(p["max"] <= 13 for p in before)
        # Its human labels come with it, clipped to the day.
        labels = graph["labels"]
        assert {label["state"] for label in labels} == {"present", "not_present"}
        assert all(start <= label["start"] < label["end"] <= start + DAY for label in labels)

    play(scenario)


def test_a_room_graph_of_a_past_day_shows_every_member_and_the_room_labels(play):
    async def scenario(home):
        start = TODAY - 2 * DAY
        graph = await home.ws(
            "room_series",
            device_ids=["kitchen_1", "kitchen_2"],
            keys=["g2_move"],
            hours=24,
            end=start + DAY,
        )
        assert set(graph["charts"]) == {"kitchen_1", "kitchen_2"}
        for member in graph["charts"].values():
            points = member["series"]["g2_move"]["points"]
            assert points[0]["t"] >= start
            assert points[-1]["t"] <= start + DAY
            assert max(point["max"] for point in points) >= 50

    play(scenario)


def test_recorded_days_and_raw_rows_read_what_was_recorded(play):
    async def scenario(home):
        days = await home.ws("recording_days", device_id="office_1", limit=100)
        listed = [item["date"] for item in days["days"]]
        assert len(listed) == real_shape.RETAINED_DAYS + 1
        page = await home.ws("recording_rows", device_id="office_1", date=_date(4), limit=500)
        assert page["total"] == real_shape.ROWS
        assert len(page["rows"]) == 500
        times = [row[0] for row in page["rows"]]
        assert times == sorted(times)
        assert TODAY - 4 * DAY <= times[0] < TODAY - 4 * DAY + real_shape.ROW_SECONDS

    play(scenario)


def test_a_graph_is_not_kept_waiting_by_a_long_learn(play):
    async def scenario(home):
        fit_running, finish_fit = asyncio.Event(), asyncio.Event()

        async def hold_learning(request):
            if request.get("operation") == "learn":
                fit_running.set()
                await finish_fit.wait()
                raise WorkerFailed(-9, b"stopped by the test: the graph has answered")

        home.transport.before = hold_learning
        await home.ws("start_learning", device_id="hall_1")
        await asyncio.wait_for(fit_running.wait(), 30)
        graph = await asyncio.wait_for(
            home.ws(
                "history_series_multi",
                device_id="office_1",
                keys=["g1_move"],
                hours=24,
                end=TODAY,
            ),
            20,
        )
        assert graph["series"]["g1_move"]["points"]
        finish_fit.set()
        await asyncio.gather(*home.runtime._learning_jobs.values(), return_exceptions=True)

    play(scenario)

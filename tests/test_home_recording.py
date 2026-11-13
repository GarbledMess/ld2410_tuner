"""Recording readings on the real-shaped home: what is delivered is what is kept, through a
restart, a crash and a failing worker."""

from datetime import UTC, datetime
from unittest.mock import patch

import real_home
import real_shape
from real_shape import NOW, ROOMS
from tuner_under_test.history import dayfile, daystore
from tuner_under_test.history.daystore import date_of
from tuner_under_test.worker import bridge
from tuner_under_test.worker.supervisor import WorkerFailed

TODAY = datetime.fromtimestamp(NOW, UTC).date().isoformat()
TODAY_START = real_shape.TODAY
GATE_1_MOVE = 2  # The column of g1_move in a raw row's values, after its start and end.


def _rows_after(page, moment):
    return [row for row in page["rows"] if row[0] >= moment]


async def _todays_rows(home, radar):
    first = await home.ws("recording_rows", device_id=radar, date=TODAY, limit=1)
    return await home.ws(
        "recording_rows",
        device_id=radar,
        date=TODAY,
        offset=max(0, first["total"] - 50),
        limit=50,
    )


def _deliver(home, radar, values):
    """Readings arrive one after another, 1.5 seconds apart, off the second."""
    delivered = []
    for value in values:
        home.clock.moved(1.5)
        home.report(radar, "g1_move", value)
        delivered.append((home.clock(), value))
    return delivered


def test_readings_survive_a_clean_restart_and_coverage_stops_at_the_unload(play):
    delivered = []

    async def record(home):
        home.clock.moved(30)
        delivered.extend(_deliver(home, "office_1", [41, 42, 43, 44, 45]))

    home = play(record)

    async def read_back(restarted):
        page = await _todays_rows(restarted, "office_1")
        kept = [
            (row[0], row[2 + real_shape.HISTORY_KEYS.index("g1_move")])
            for row in _rows_after(page, delivered[0][0])
        ]
        assert kept == delivered
        # Nothing is known after the unload: time since then is a gap, not the last reading.
        restarted.clock.moved(600)
        assert page["rows"][-1][1] == delivered[-1][0]

    second = real_home.Home(home.config)
    second.clock.now = home.clock.now
    with real_home.worker_on_transport(second):
        real_home.run_on_clock(second.clock, lambda: real_home.lifetime(second, read_back))


def test_a_crash_keeps_what_was_flushed_and_ends_coverage_there(play, tmp_path):
    crashed = tmp_path / "after_the_crash"
    delivered = []

    async def record_then_crash(home):
        home.clock.moved(30)
        delivered.extend(_deliver(home, "office_1", [61, 62, 63]))
        # The flush timer fires with nothing else recorded: the last reading is its last record.
        home.runtime._schedule_flush()
        await home.flushed()
        # The power goes: the disk as it is now is all the next start has.
        real_shape.open_copy(home.config, crashed)

    home = play(record_then_crash)

    async def read_back(restarted):
        page = await _todays_rows(restarted, "office_1")
        kept = _rows_after(page, delivered[0][0])
        assert [row[0] for row in kept] == [moment for moment, _ in delivered]
        assert kept[-1][1] == delivered[-1][0]
        graph = await restarted.ws(
            "history_series_multi",
            device_id="office_1",
            keys=["g1_move"],
            hours=1,
            end=delivered[-1][0] + 1200,
        )
        points = graph["series"]["g1_move"]["points"]
        assert max(point["end"] for point in points) <= delivered[-1][0]

    second = real_home.Home(crashed)
    second.clock.now = home.clock.now + 1200
    with real_home.worker_on_transport(second):
        real_home.run_on_clock(second.clock, lambda: real_home.lifetime(second, read_back))


def test_a_radar_with_recording_off_records_nothing_until_it_is_switched_back_on(play):
    async def scenario(home):
        radar = "garage_1"
        assert ROOMS["garage"][1] == 1
        snapshot = await home.ws("snapshot")
        assert snapshot["devices"][radar]["recording_enabled"] is False
        before = (await _todays_rows(home, radar))["total"]
        _deliver(home, radar, [71, 72])
        await home.save()
        assert (await _todays_rows(home, radar))["total"] == before

        await home.ws("set_recording", device_id=radar, enabled=True)
        _deliver(home, radar, [73, 74])
        await home.save()
        assert (await _todays_rows(home, radar))["total"] > before

    play(scenario)


def test_recording_continues_through_a_failing_worker_and_nothing_is_lost(play):
    async def scenario(home):
        runtime = home.runtime
        delivered = _deliver(home, "office_1", [31, 32])

        async def broken(request):
            if request.get("operation") in ("flush", "save"):
                raise WorkerFailed(-9, b"killed by the memory limit")

        def full(log, data):  # Plain appends are written in-process; the disk fails too.
            log.write(data[:3])
            raise OSError("No space left on device")

        home.transport.before = broken
        with patch.object(daystore, "_write_bytes", side_effect=full):
            await home.poll()
            await home.flushed()
            stalled = runtime.snapshot()["history_save"]
            assert stalled["waiting_since"] is not None
            # Writes keep failing; readings keep arriving and are kept for the next flush.
            delivered += _deliver(home, "office_1", [33, 34])
            await home.poll()
            await home.flushed()
        home.transport.before = None
        delivered += _deliver(home, "office_1", [35])
        await home.save()
        assert runtime.snapshot()["history_save"]["waiting_since"] is None
        page = await _todays_rows(home, "office_1")
        assert [row[0] for row in _rows_after(page, delivered[0][0])] == [
            moment for moment, _ in delivered
        ]

    play(scenario)


def test_recording_is_saved_directly_once_the_worker_is_quarantined(play):
    async def scenario(home):
        runtime = home.runtime
        delivered = _deliver(home, "office_1", [51, 52])

        # A cleanup that could not be proven closes the worker for good; recording goes on.
        supervisor = bridge._service(runtime)
        supervisor._closed = True
        # A day still to finish needs the worker; the direct route must finish it instead.
        yesterday = real_shape.TODAY - 86400
        runtime._days.append("unfinished", dayfile.Change(yesterday + 60, {0: 20}))
        await home.poll()
        await home.flushed()
        assert runtime._days._path("unfinished", date_of(yesterday), ".day").exists()
        assert not runtime._days._live["office_1"].buffer
        recorded = (runtime._days.folder("office_1") / f"{TODAY}.log").read_bytes()
        records = dayfile.decode_log(recorded, TODAY_START)
        assert [record.time for record in records[-2:]] == [moment for moment, _ in delivered]

    play(scenario)

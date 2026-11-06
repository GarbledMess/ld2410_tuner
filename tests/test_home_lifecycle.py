"""Starting and stopping on the real-shaped home: what a crash leaves, and what unload saves."""

import asyncio
import datetime

import real_home
import real_shape
from real_shape import DAY, TODAY
from tuner_under_test.history import dayfile
from tuner_under_test.worker.supervisor import WorkerFailed

RADAR = "office_1"
HOME_DOMAIN = real_shape.DOMAIN


def _history(home):
    return home.config / real_shape.STORE_FOLDER / HOME_DOMAIN / "history"


def _date(ago):
    return datetime.datetime.fromtimestamp(TODAY - ago * DAY, datetime.UTC).date()


def _crash_leftovers(home):
    """What the disk holds when the power went during a flush, a finish and a publication."""
    folder = _history(home) / RADAR
    today = folder / f"{_date(0).isoformat()}.log"
    # Half a record at the end of today's log: the append the crash interrupted.
    today.write_bytes(today.read_bytes() + bytes([dayfile.CHANGE, 5, 1]))
    # Yesterday never got finished into its day file; only its log exists.
    yesterday = dayfile.decode_day((folder / f"{_date(1).isoformat()}.day").read_bytes())
    (folder / f"{_date(1).isoformat()}.day").unlink()
    start = TODAY - DAY
    records = [
        dayfile.Change(yesterday.times[row], dict(enumerate(yesterday.vector(row))))
        for row in range(0, 2000)
    ]
    (folder / f"{_date(1).isoformat()}.log").write_bytes(dayfile.encode_records(records, start))
    # Temporary files of an interrupted finish, and of a publication.
    (folder / f"{_date(2).isoformat()}.tmp").write_bytes(b"half a file")
    (_history(home) / "leftover.tmp").write_bytes(b"half a publication")


def test_startup_recovers_from_what_a_crash_leaves_on_disk(play):
    async def scenario(home):
        folder = _history(home) / RADAR
        assert not list(folder.glob("*.tmp"))
        assert not list(_history(home).glob("*.tmp"))
        # Yesterday's log is now its finished day; coverage ended at its last record.
        assert (folder / f"{_date(1).isoformat()}.day").exists()
        assert not (folder / f"{_date(1).isoformat()}.log").exists()
        day = dayfile.decode_day((folder / f"{_date(1).isoformat()}.day").read_bytes())
        assert len(day.times) == 2000
        assert day.ends[-1] == day.times[-1]
        # Today's log kept every whole record and now ends coverage at the last of them.
        page = await home.ws("recording_rows", device_id=RADAR, date=_date(0).isoformat(), limit=1)
        assert page["total"] == (real_shape.NOW - TODAY) // real_shape.PARTIAL_SECONDS
        last = await home.ws(
            "recording_rows",
            device_id=RADAR,
            date=_date(0).isoformat(),
            offset=page["total"] - 1,
            limit=1,
        )
        assert last["rows"][0][1] == last["rows"][0][0]

    play(scenario, prepare=_crash_leftovers, whole=True)


def test_a_new_home_with_no_saved_data_starts_and_finds_its_radars(play):
    def nothing_saved(home):
        real_shape.shutil.rmtree(home.config / real_shape.STORE_FOLDER)

    async def scenario(home):
        snapshot = await home.ws("snapshot")
        assert set(snapshot["devices"]) == set(real_shape.radars())
        assert {device["recording_enabled"] for device in snapshot["devices"].values()} == {False}

    play(scenario, prepare=nothing_saved)


def test_shutdown_saves_directly_when_the_worker_cannot_do_its_final_save(play):
    low, high = TODAY - DAY + 100.5, TODAY - DAY + 200.25
    delivered = []

    async def scenario(home):
        await home.ws(
            "label_room_range", group_id="area:kitchen", start=low, end=high, state="present"
        )
        home.clock.moved(5)
        home.report(RADAR, "g1_move", 77)
        delivered.append(home.clock())

        async def broken(_request):
            raise WorkerFailed(-9, b"killed")

        home.transport.before = broken

    home = play(scenario)

    async def read_back(restarted):
        labels = restarted.runtime.data["room_labels"]["area:kitchen"]
        assert any(label["start"] == low for label in labels)
        total = await restarted.ws(
            "recording_rows", device_id=RADAR, date=_date(0).isoformat(), limit=1
        )
        last = await restarted.ws(
            "recording_rows",
            device_id=RADAR,
            date=_date(0).isoformat(),
            offset=total["total"] - 1,
            limit=1,
        )
        assert last["rows"][0][0] == delivered[0]

    second = real_home.Home(home.config)
    second.clock.now = home.clock.now
    with real_home.worker_on_transport(second):
        real_home.run_on_clock(second.clock, lambda: real_home.lifetime(second, read_back))


def test_unloading_during_a_fit_reports_it_interrupted_and_keeps_the_rest(play):
    async def scenario(home):
        in_fit, never = asyncio.Event(), asyncio.Event()

        async def endless(request):
            if request.get("operation") == "learn":
                in_fit.set()
                await never.wait()
                raise WorkerFailed(-15, b"terminated by its supervisor")

        home.transport.before = endless
        await home.ws("start_learning", device_id="hall_1")
        await asyncio.wait_for(in_fit.wait(), 60)
        # Unload does not wait for the fit: its supervisor stops the worker.
        runtime = home.runtime
        stopping = asyncio.create_task(home.stop())
        await asyncio.sleep(0.05)
        never.set()
        await asyncio.wait_for(stopping, 60)
        assert runtime.data["devices"]["hall_1"]["learning_job"]["status"] == "interrupted"

    play(scenario)

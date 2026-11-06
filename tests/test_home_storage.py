"""Trim and day-long retention cleanup on the real-shaped home: batches, and what must survive."""

import asyncio
import datetime
import json
from unittest.mock import patch

import pytest
import real_shape
from real_home import WebsocketError
from real_shape import DAY, RETAINED_DAYS, TODAY
from tuner_under_test.history import recording

RADAR = "office_1"


def _date(ago):
    return datetime.datetime.fromtimestamp(TODAY - ago * DAY, datetime.UTC).date().isoformat()


async def _rows(home, ago):
    first = await home.ws("recording_rows", device_id=RADAR, date=_date(ago), limit=1)
    return first["total"]


async def _rows_within(home, start, end):
    """Recorded rows of the radar overlapping ``[start, end)``, a day at a time."""
    date = datetime.datetime.fromtimestamp(start, datetime.UTC).date().isoformat()
    offset, found = 0, 0
    while offset is not None:
        page = await home.ws(
            "recording_rows", device_id=RADAR, date=date, offset=offset, limit=10000
        )
        found += sum(row[0] < end and row[1] > start for row in page["rows"])
        offset = page["next_offset"]
    return found


async def _cleanup_comes_due(home):
    """The startup cleanup's turn comes, without its minute's rest between durable batches."""
    real_sleep = asyncio.sleep

    async def no_rest(seconds):
        await real_sleep(0 if seconds == 60 else seconds)

    home.hass.maintenance_due.set()
    with patch.object(recording.asyncio, "sleep", no_rest):
        await home.runtime._cleanup_task
    return sum(request.get("operation") == "maintenance" for request in home.transport.requests)


def _only_some_history(home, days):
    """This radar keeps only the days ``days`` back: enough to prove the rule, without minutes
    of the same rule on every day of every radar."""
    history = home.config / real_shape.STORE_FOLDER / real_shape.DOMAIN / "history"
    for radar, room in real_shape.radars().items():
        if radar != RADAR and real_shape.recorded_days(room) < RETAINED_DAYS:
            for path in (history / radar).glob("*.day"):
                path.unlink()
    for ago in range(1, RETAINED_DAYS + 1):
        if ago not in days:
            (history / RADAR / f"{_date(ago)}.day").unlink()


def _automatic_expiry_shortened_a_day_ago(home):
    """The owner cut automatic retention to 30 days; tonight's cleanup has not yet run."""
    _only_some_history(home, {3, *range(10, 26), *range(31, 43)})
    path = home.config / real_shape.STORE_FOLDER / f"{real_shape.DOMAIN}.data"
    saved = json.loads(path.read_text(encoding="utf8"))
    saved["data"]["storage_settings"].update(thin_after_days=29, automatic_days=30)
    for device in saved["data"]["devices"].values():
        device["history_cleanup"]["timestamp"] -= 2 * DAY
    path.write_text(json.dumps(saved), encoding="utf8")


def test_retention_cleanup_works_through_the_days_in_batches_and_keeps_human_labelled_time(play):
    long_label = next(
        label
        for label in real_shape.human_labels("office")
        if label["state"] == "present" and 40 * DAY < TODAY - label["start"] < 42 * DAY
    )

    async def scenario(home):
        runtime = home.runtime
        labelled_before = await _rows_within(home, long_label["start"], long_label["end"])
        assert labelled_before > 100
        automatic_label_count = len(runtime.data["room_labels"]["area:office"])

        batches = await _cleanup_comes_due(home)
        assert batches > 1, "A day-long cleanup must give the worker back between batches"
        assert not runtime.data.get("maintenance_progress")

        # Automatic time older than its retention is gone, whole days of it...
        assert (await _rows(home, 42)) == 0
        assert (await _rows(home, 35)) == 0
        # ... the newest days are not touched ...
        assert (await _rows(home, 3)) == real_shape.rows_on(RADAR, 3)
        assert (await _rows(home, 25)) == real_shape.rows_on(RADAR, 25)
        # ... and time a person labelled stays for its own, longer retention.
        assert await _rows_within(home, long_label["start"], long_label["end"]) == labelled_before
        # Sensor labels are from the last two weeks and stay with the days they describe.
        assert len(runtime.data["room_labels"]["area:office"]) == automatic_label_count

    play(scenario, prepare=_automatic_expiry_shortened_a_day_ago)


def test_trim_to_a_size_removes_unguessed_time_oldest_first_and_saves_human_labels(play):
    async def scenario(home):
        runtime = home.runtime
        before = await home.hass.async_add_executor_job(runtime._days.size)
        label = next(
            item
            for item in runtime.data["devices"][RADAR]["history_labels"]
            if item["state"] == "present"
            and item["end"] - item["start"] > 600
            and TODAY - item["start"] < 4 * DAY
        )
        labelled_before = await _rows_within(home, label["start"], label["end"])
        target = int(before / 1024 / 1024 * 0.9)
        report = await home.ws("trim_storage", target_mib=target)
        assert report["removed_automatic_seconds"] > 0
        assert report["removed_human_seconds"] == 0
        assert await home.hass.async_add_executor_job(runtime._days.size) <= target * 1024 * 1024
        assert await _rows_within(home, label["start"], label["end"]) == labelled_before
        # What went first was the oldest.
        assert (await _rows(home, 1)) >= (await _rows(home, 12))
        assert (await _rows(home, 12)) < real_shape.rows_on(RADAR, 12)

    play(scenario, prepare=lambda home: _only_some_history(home, range(1, 13)))


def test_an_impossible_storage_setting_is_refused_and_changes_nothing(play):
    async def scenario(home):
        before = dict(home.runtime.data["storage_settings"])
        with pytest.raises(WebsocketError, match="expiry"):
            await home.ws(
                "configure_storage",
                settings={**real_shape.SETTINGS, "automatic_days": 90, "human_days": 30},
            )
        assert home.runtime.data["storage_settings"] == before

    play(scenario)

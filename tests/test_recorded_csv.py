"""A day's CSV is written by the server in one pass, byte for byte as the panel made it.

The panel built the CSV from 10,000-row pages: on a busy day 15 sequential worker processes,
each decoding the whole day, for a 14.5 MB file. The server now writes the file once and the
panel downloads it. Its text must be what the panel's ``rawCsvPage`` produced: JavaScript
timestamps, rounding and number formatting, CRLF lines.
"""

import io
import json
from pathlib import Path

import test_tuner  # noqa: F401
from tuner_under_test.presentation import recorded_csv

GOLDEN = json.loads(
    (Path(__file__).parent / "fixtures" / "recorded_csv_golden.json").read_text(encoding="utf-8")
)


def test_rows_are_written_exactly_as_the_panel_wrote_them():
    out = io.StringIO(newline="")
    recorded_csv.write_rows(out, GOLDEN["rows"], block=7)
    assert recorded_csv.HEADER + out.getvalue() == GOLDEN["csv"]


def test_numbers_follow_javascript():
    cases = {
        0: "0",
        42.0: "42",
        12.5: "12.5",
        1e-5: "0.00001",
        1e-7: "1e-7",
        2.5e-6: "0.0000025",
        1e16: "10000000000000000",
        1.5e21: "1.5e+21",
        1e21: "1e+21",
        123456789012.5: "123456789012.5",
        0.1 + 0.2: "0.30000000000000004",
        -0.5: "-0.5",
        12.300000190734863: "12.300000190734863",
    }
    for value, text in cases.items():
        assert recorded_csv.js_number(value) == text, value


def test_a_day_is_written_as_its_old_pages_were(tmp_path):
    import random
    import time
    import types

    from tuner_under_test.history import dayfile
    from tuner_under_test.history.daystore import DayStore, bounds, date_of
    from tuner_under_test.worker import recordings

    rng = random.Random(11)
    store = DayStore(tmp_path)
    moment = bounds(date_of(time.time()))[0] + 30
    for n in range(20000):
        moment += rng.choice((0.0005, 0.04, 1.5))
        if n % 997 == 0:
            store.append("r", dayfile.End(moment))
            continue
        gates = {g: rng.randint(0, 100) for g in rng.sample(range(18), 3)}
        if n % 50 == 0:
            gates[5] = rng.uniform(0, 100)  # Widens gate 5 to float32.
        if n % 70 == 0:
            gates[7] = None
        store.append("r", dayfile.Change(moment, gates))
    date = date_of(moment)
    end = moment - 100.25  # A pinned end inside the day clips the readings at it.
    view = types.SimpleNamespace(_days=store)
    rows, offset = [], 0
    while offset is not None:
        page = recordings.calculate(view, "r", date.isoformat(), offset, 3000, end, 0)
        rows += page["rows"]
        offset = page["next_offset"]
    old = io.StringIO(newline="")
    recorded_csv.write_rows(old, rows)
    new = io.StringIO(newline="")
    count = recorded_csv.write_day(new, store.day("r", date, until=end), end)
    assert count == len(rows) > 15000
    assert new.getvalue() == old.getvalue()


def test_the_csv_download_is_the_day_the_pages_showed(play):
    import datetime

    import real_shape
    from tuner_under_test.presentation import recorded_csv as module

    today = datetime.datetime.fromtimestamp(real_shape.TODAY, datetime.UTC).date().isoformat()
    past = datetime.date.fromisoformat(today) - datetime.timedelta(days=3)

    async def scenario(home):
        for date in (today, past.isoformat()):
            first = await home.ws("recording_rows", device_id="office_1", date=date, limit=1)
            rows, offset = [], 0
            while offset is not None:
                page = await home.ws(
                    "recording_rows",
                    device_id="office_1",
                    date=date,
                    offset=offset,
                    limit=10000,
                    end=first["end"],
                    revision=first["revision"],
                )
                rows += page["rows"]
                offset = page["next_offset"]
            exported = await home.ws(
                "recording_csv",
                device_id="office_1",
                date=date,
                end=first["end"],
                revision=first["revision"],
            )
            assert exported["filename"] == f"ld2410-office_1-{date}.csv"
            assert exported["rows"] == len(rows) > 0
            token = exported["url"].split("/")[-1].split("?")[0]
            folder = home.config / real_shape.STORE_FOLDER / real_shape.DOMAIN / "exports"
            written = (folder / f"{token}.csv").read_bytes().decode("utf-8")
            expected = io.StringIO(newline="")
            expected.write(module.HEADER)
            module.write_rows(expected, rows)
            assert written == expected.getvalue()
            assert exported["bytes"] == len(written.encode())

    play(scenario)


def test_a_changed_recording_is_refused_rather_than_exported(play):
    import datetime

    import pytest
    import real_shape
    from real_home import WebsocketError

    today = datetime.datetime.fromtimestamp(real_shape.TODAY, datetime.UTC).date().isoformat()

    async def scenario(home):
        first = await home.ws("recording_rows", device_id="office_1", date=today, limit=1)
        with pytest.raises(WebsocketError, match="Recordings changed"):
            await home.ws(
                "recording_csv",
                device_id="office_1",
                date=today,
                end=first["end"],
                revision=first["revision"] + 1,
            )

    play(scenario)


async def _offloaded():
    import threading

    from tuner_under_test.history import pace

    return await pace.offload(lambda: threading.current_thread().name)


def test_offloaded_work_runs_on_the_integrations_own_thread():
    import asyncio

    assert asyncio.run(_offloaded()).startswith("ld2410-background")


def test_a_file_export_never_borrows_a_home_assistant_executor_thread(tmp_path):
    import asyncio
    import types
    from unittest.mock import patch

    from tuner_under_test.presentation import export_files

    def refuse(*_args):
        raise AssertionError("an HA executor thread was borrowed")

    async def scenario():
        hass = types.SimpleNamespace(
            async_add_executor_job=refuse,
            config=types.SimpleNamespace(path=lambda *parts: str(tmp_path.joinpath(*parts))),
        )
        runtime = types.SimpleNamespace(hass=hass, _export_file=None)

        def write(path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"a,b\r\n")
            return path.stat().st_size

        with patch.object(export_files, "async_sign_path", lambda _h, path, _e: path):
            result = await export_files.publish(runtime, "x.csv", ".csv", write)
        runtime._export_file["timer"].cancel()
        return result

    result = asyncio.run(scenario())
    assert result["bytes"] == 5 and result["filename"] == "x.csv"

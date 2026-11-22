"""A chosen history folder (USB drive, SD card, network share): moving into and out of it,
refusing to write while it is not mounted, and the options form's checks."""

import asyncio
import tempfile
import time
import types
import unittest
from pathlib import Path

import test_tuner  # noqa: F401
from tuner_under_test.history import dayfile, location
from tuner_under_test.history.daystore import DayStore


class _Config:
    def __init__(self, folder):
        self.config_dir = str(folder)

    def path(self, *parts):
        return str(Path(self.config_dir, *parts))


class _Hass:
    def __init__(self, folder):
        self.config = _Config(folder)

    async def async_add_executor_job(self, function, *args):
        return function(*args)


class _Store:
    def __init__(self, fail=False):
        self.saved, self.fail = [], fail

    async def async_save(self, data):
        if self.fail:
            raise OSError("disk full")
        self.saved.append(dict(data))


def _record(store, key="radar"):
    store.append(key, dayfile.Change(time.time(), dict.fromkeys(range(18), 5)))
    store.flush()


class LocationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.config, self.drive = base / "config", base / "media" / "usb"
        self.config.mkdir()
        self.drive.mkdir(parents=True)
        self.hass = _Hass(self.config)

    def tearDown(self):
        self.temporary.cleanup()

    def activate(self, data, options, store=None):
        store = store or _Store()
        return asyncio.run(location.activate(self.hass, store, data, options)), store

    def test_default_location_is_home_assistant_storage(self):
        (root, marker), store = self.activate({}, {})
        self.assertEqual(root, self.config / ".storage" / "ld2410_tuner" / "history")
        self.assertIsNone(marker)
        self.assertEqual(store.saved, [])

    def test_history_moves_to_a_chosen_folder_and_back(self):
        old = DayStore(location.root(self.hass, ""))
        _record(old)
        before = sorted(p.relative_to(old.root) for p in old.root.rglob("*") if p.is_file())
        data = {"devices": {}}
        (root, marker), store = self.activate(data, {location.OPTION: str(self.drive)})
        self.assertEqual(root, self.drive / "ld2410_tuner" / "history")
        self.assertTrue(marker.is_file())
        self.assertEqual(data[location.DATA_KEY], str(self.drive))
        self.assertEqual(store.saved[-1][location.DATA_KEY], str(self.drive))
        after = sorted(p.relative_to(root) for p in root.rglob("*") if p.is_file())
        self.assertEqual(before, after)
        self.assertFalse(old.root.exists())
        moved = DayStore(root, marker=marker)
        moved.open(time.time())
        self.assertIsNotNone(moved.last("radar"))

        (back, marker), _store = self.activate(data, {location.OPTION: ""})
        self.assertIsNone(marker)
        self.assertEqual(
            before, sorted(p.relative_to(back) for p in back.rglob("*") if p.is_file())
        )
        self.assertFalse((self.drive / "ld2410_tuner").exists())

    def test_not_moving_leaves_the_old_history_and_starts_in_the_new_folder(self):
        old = DayStore(location.root(self.hass, ""))
        _record(old)
        options = {location.OPTION: str(self.drive), location.MOVE_OPTION: False}
        (root, marker), _store = self.activate({}, options)
        self.assertTrue(marker.is_file())
        self.assertFalse(root.exists())
        self.assertTrue(any(old.root.rglob("*.log")))

    def test_a_failed_move_keeps_the_history_where_it_was(self):
        old = DayStore(location.root(self.hass, ""))
        _record(old)
        occupied = self.drive / "ld2410_tuner" / "history"
        occupied.mkdir(parents=True)
        (occupied / "someone-else").write_text("x")
        data = {}
        with self.assertLogs(level="ERROR"):
            (root, marker), _store = self.activate(data, {location.OPTION: str(self.drive)})
        self.assertEqual(root, old.root)
        self.assertIsNone(marker)
        self.assertNotIn(location.DATA_KEY, data)
        self.assertTrue(any(old.root.rglob("*.log")))

    def test_a_failed_save_keeps_the_old_copy_and_the_next_start_copies_again(self):
        old = DayStore(location.root(self.hass, ""))
        _record(old)
        data = {}
        options = {location.OPTION: str(self.drive)}
        with self.assertLogs(level="ERROR"):
            (root, _marker), _store = self.activate(data, options, _Store(fail=True))
        self.assertEqual(root, old.root)
        self.assertEqual(data.get(location.DATA_KEY, ""), "")
        self.assertTrue(any(old.root.rglob("*.log")))
        (root, _marker), _store = self.activate(data, options)
        self.assertEqual(root, self.drive / "ld2410_tuner" / "history")
        self.assertTrue(any(root.rglob("*.log")))

    def test_an_unmounted_folder_is_never_written(self):
        data = {}
        (root, marker), _store = self.activate(data, {location.OPTION: str(self.drive)})
        store = DayStore(root, marker=marker)
        marker.unlink()  # The drive went away: its mount point is an empty folder.
        store.append("radar", dayfile.Change(time.time(), dict.fromkeys(range(18), 5)))
        with self.assertRaises(location.LocationUnavailable):
            store.flush()
        from tuner_under_test.worker import flushing

        with self.assertRaises(location.LocationUnavailable):
            flushing.append_logs(store, time.time())
        self.assertEqual(list(root.rglob("*.log")), [])
        with self.assertRaises(location.LocationUnavailable):
            self.activate(data, {location.OPTION: str(self.drive)})

    def test_worker_transactions_refuse_an_unmounted_folder(self):
        from tuner_under_test.worker import transactions

        marker = self.drive / "ld2410_tuner" / location.MARKER
        with self.assertRaises(location.LocationUnavailable):
            transactions._prepare(
                {}, self.drive / "ld2410_tuner" / "history", "flush", {}, 0, None,
                __import__("threading").RLock(), marker,
            )  # fmt: skip
        self.assertFalse((self.drive / "ld2410_tuner").exists())

    def test_options_form_checks(self):
        check = location.check
        self.assertEqual(check(self.hass, "  "), "")
        self.assertEqual(check(self.hass, str(self.drive) + "/"), str(self.drive))
        self.assertFalse((self.drive / "ld2410_tuner" / ".ld2410_probe").exists())
        cases = {
            "relative/path": "not_absolute",
            str(self.drive / "missing"): "missing",
            str(self.config / ".storage" / "x"): "inside_storage",
        }
        for value, reason in cases.items():
            with self.subTest(value=value), self.assertRaises(location.LocationError) as caught:
                check(self.hass, value)
            self.assertEqual(caught.exception.reason, reason)

    def test_recorded_data_reports_the_folder(self):
        from tuner_under_test.presentation import recordings

        runtime = types.SimpleNamespace(_days=DayStore(self.drive / "h"), data={"devices": {}})
        self.assertEqual(
            recordings.recording_devices(runtime)["storage"]["location"], str(self.drive / "h")
        )


if __name__ == "__main__":
    unittest.main()

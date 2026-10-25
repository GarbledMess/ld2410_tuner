"""Offline replay, quality-gate, package and release transport regressions."""

import argparse
import contextlib
import datetime
import importlib
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
replay = importlib.import_module("replay_autolabels")
release = importlib.import_module("release")
validator = importlib.import_module("validate_package")
complexity = importlib.import_module("check_complexity")
updater = importlib.import_module("update_characterisation")


START = 1_760_486_400.0  # A UTC midnight: the recording is one day file.


def record(history, device_id, count=100):
    """Write ``count`` six-second readings to ``device_id``'s day file under ``history``:
    every gate at 5 for the first half, then 30."""
    dayfile, daystore = replay.load("history.dayfile"), replay.load("history.daystore")
    records = [
        dayfile.Change(START, dict.fromkeys(range(18), 5)),
        dayfile.Change(START + count // 2 * 6, dict.fromkeys(range(18), 30)),
        dayfile.End(START + count * 6),
    ]
    start, end = daystore.bounds(daystore.date_of(START))
    folder = history / daystore._folder_name(device_id)
    folder.mkdir(parents=True)
    day = dayfile.day_from_log(records, None, start, end)
    (folder / f"{daystore.date_of(START).isoformat()}.day").write_bytes(dayfile.encode_day(day))
    return folder


class ReplayTests(unittest.TestCase):
    def test_replay_reports_are_anonymous_and_never_change_source_data(self):
        device = {
            "history_labels": [],
            "entities": {"sensor.synthetic": {"gate": 0, "kind": "move"}},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root / "store.json"
            history = root / "ld2410_tuner" / "history"
            day = next(record(history, "private_device").iterdir())
            labels = root / "labels.json"
            output = root / "output.json"
            store.write_text(
                json.dumps({"data": {"devices": {"private_device": device, "empty": {}}}})
            )
            labels.write_text(
                json.dumps(
                    {
                        "devices": {
                            "private_device": {
                                "history": {
                                    "labels": [
                                        {
                                            "start": START,
                                            "end": START + 300,
                                            "state": "not_present",
                                        },
                                        {
                                            "start": START + 300,
                                            "end": START + 600,
                                            "state": "present",
                                        },
                                    ]
                                }
                            }
                        }
                    }
                )
            )
            before = store.read_bytes(), labels.read_bytes(), day.read_bytes()
            args = argparse.Namespace(
                store=store,
                history=None,  # Beside the store, as Home Assistant keeps it.
                labels=labels,
                output=output,
                baseline=replay.PACKAGE / "presence" / "inference.py",
            )
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                reports = replay.run(args)
            self.assertEqual(len(reports), 3)
            self.assertEqual(json.loads(output.read_text()), reports)
            self.assertNotIn("private_device", output.read_text() + stdout.getvalue())
            self.assertEqual(before, (store.read_bytes(), labels.read_bytes(), day.read_bytes()))
            self.assertEqual([path.name for path in day.parent.iterdir()], [day.name])
            for report in reports:
                self.assertEqual(report["before"], report["after"])
                self.assertEqual(sum(report["after"]["validation"]["matrix"].values()), 20)

    def test_a_day_still_being_recorded_ends_at_its_last_record(self):
        """A log continues the finished day before it, and is read up to its last record
        (as the integration recovers it), never to midnight."""
        dayfile, daystore = replay.load("history.dayfile"), replay.load("history.daystore")
        with tempfile.TemporaryDirectory() as directory:
            folder = record(Path(directory), "radar", count=14400)  # Held to midnight.
            date = daystore.date_of(START) + datetime.timedelta(days=1)
            start = daystore.bounds(date)[0]
            log = folder / f"{date.isoformat()}.log"
            written = dayfile.encode_records([dayfile.Change(start + 12, {0: 9})], start)
            log.write_bytes(written)
            days = replay.recorded_days(Path(directory), "radar")
            self.assertEqual(log.read_bytes(), written)  # Read, never recovered in place.
            rows = replay.decode_rows({"history_labels": []}, days[1:])
        self.assertEqual(
            [(ts - start, values["g0_move"]) for ts, values, _ in rows], [(0, 30), (6, 30)]
        )
        self.assertEqual(rows[0][1]["g1_still"], 30)

    def test_future_holdout_labels_cannot_train_the_estimator(self):
        class Model:
            seen = []

            @staticmethod
            def estimate_presence(values, references, *args):
                Model.seen.append(sum(references.get("g0_move", {}).get("present", [])))
                return {
                    "label": "unknown",
                    "score": 0,
                    "confidence": 0,
                    "presence_probability": 0.5,
                }

        rows = [(i * 6, {"g0_move": 30}, "present") for i in range(10)]
        rows += [(60, {"g0_move": 4}, None)]
        replay.evaluate(rows, ["g0_move"], Model, "causal")
        self.assertEqual(Model.seen, [0, 1, 2, 3, 4, 5, 6, 7, 8, 8])

    def test_metrics_keep_false_bursts_and_unknown_presence_separate(self):
        metrics = replay.Metrics()
        observations = [
            (0, "not_present", "present"),
            (6, "not_present", "present"),
            (12, "not_present", "unknown"),
            (18, "not_present", "present"),
            (24, "present", "unknown"),
            (30, "present", "not_present"),
            (36, "present", "present"),
        ]
        for timestamp, truth, guess in observations:
            metrics.add(timestamp, truth, guess, 0.6)
        summary = metrics.summary()
        self.assertEqual(summary["false_bursts"], 2)
        self.assertEqual(summary["false_bursts_per_empty_hour"], 300)
        self.assertEqual(summary["longest_missed_presence_run"], 2)
        self.assertEqual(summary["confidence"]["0.6"], {"count": 5, "correct": 1})

    def test_legacy_confirmation_and_invalid_blocks(self):
        result = {"label": "present", "score": 2}
        state = {}
        self.assertEqual(
            [replay.confirm_legacy(result, state, i) for i in range(3)],
            ["unknown", "unknown", "present"],
        )
        self.assertEqual(replay.confirm_legacy({"label": "unknown"}, state, 4), "unknown")
        self.assertEqual(
            replay.confirm_legacy({"label": "present", "score": 0}, state, 5), "unknown"
        )
        with tempfile.TemporaryDirectory() as directory:
            day = next(record(Path(directory), "radar").iterdir())
            day.write_bytes(b"corrupt")
            with self.assertRaisesRegex(ValueError, "Not a recorded day file"):
                replay.recorded_days(Path(directory), "radar")
        histogram = [100] * 101
        replay.compress(histogram)
        self.assertEqual(histogram, [50] * 101)


class PackageTests(unittest.TestCase):
    def test_validate_real_package(self):
        validator.validate(ROOT)
        package = ROOT / "custom_components" / validator.DOMAIN
        assets = {
            path.relative_to(package).as_posix()
            for path in package.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }
        self.assertEqual(assets, set(validator.RUNTIME_FILES))

    def test_duplicate_runtime_files_are_rejected(self):
        self.assertEqual(len(validator.RUNTIME_FILES), len(set(validator.RUNTIME_FILES)))
        doubled = validator.RUNTIME_FILES + validator.RUNTIME_FILES[:1]
        with patch.object(validator, "RUNTIME_FILES", doubled):
            with self.assertRaisesRegex(ValueError, "Duplicate integration file: __init__.py"):
                validator.validate(ROOT)

    def test_invalid_hacs_metadata_and_json_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "hacs.json"
            for value in [
                [],
                {},
                {"name": "x", "content_in_root": True},
                {"name": "x", "render_readme": "yes"},
                {"name": "x"},
            ]:
                path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    validator._validate_hacs(root)
            components = root / "custom_components"
            components.mkdir()
            with self.assertRaisesRegex(ValueError, "Expected only"):
                validator.validate(root)
            package = components / validator.DOMAIN
            package.mkdir()
            path.write_text((ROOT / "hacs.json").read_text())
            with self.assertRaisesRegex(ValueError, "Missing integration file"):
                validator.validate(root)
            manifest = package / "manifest.json"
            for value in [{"domain": "wrong"}, {"domain": validator.DOMAIN}]:
                manifest.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    validator._validate_manifest(package)

    def test_complexity_gate_catches_nested_functions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tools").mkdir()
            (root / "tools" / "example.py").write_text(
                "def outer(x):\n    def inner():\n        if x:\n            return 1\n    return inner()\n"
            )
            scores = complexity.measure(root)
            self.assertEqual({row[2] for row in scores}, {"outer", "inner"})
        with patch.object(complexity, "measure", return_value=[("test.py", 1, "deep", 11)]):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(complexity.main())


class TransportTests(unittest.TestCase):
    def test_transport_uses_fixed_hosts_no_redirects_and_redacts_errors(self):
        api = release.GitHub("Example/radar", "synthetic-token")
        opener = Mock()
        response = io.StringIO('{"id": 1}')
        opener.open.return_value = contextlib.nullcontext(response)
        with patch.object(release.urllib.request, "build_opener", return_value=opener):
            self.assertEqual(api.request("/releases", "POST", {"draft": True}), {"id": 1})
            request = opener.open.call_args.args[0]
            self.assertEqual(
                request.full_url, "https://api.github.com/repos/Example/radar/releases"
            )
            self.assertEqual(json.loads(request.data), {"draft": True})
            opener.open.side_effect = urllib.error.HTTPError("url", 404, "secret body", {}, None)
            self.assertIsNone(api.request("/missing", missing_ok=True))
            with self.assertRaisesRegex(RuntimeError, "HTTP 404") as error:
                api.request("/missing")
            self.assertNotIn("secret", str(error.exception))
        self.assertIsNone(
            release.NoRedirect().redirect_request(None, None, 302, "", {}, "elsewhere")
        )
        with self.assertRaises(ValueError):
            release.GitHub("bad/repo/path", "synthetic-token")

    def test_release_without_version_change_performs_no_build_or_write(self):
        with (
            patch.dict(
                release.os.environ,
                {
                    "GITHUB_EVENT_NAME": "push",
                    "GITHUB_REF": "refs/heads/main",
                    "GITHUB_SHA": "a" * 40,
                    "PREVIOUS_SHA": "b" * 40,
                    "GITHUB_REPOSITORY": "owner/repo",
                    "GH_TOKEN": "synthetic",
                },
            ),
            patch.object(sys, "argv", ["release.py", "--publish"]),
            patch.object(release, "git", return_value="a" * 40),
            patch.object(release, "changed_version", return_value=None),
            # The unchanged version is already published.
            patch.object(
                release, "GitHub", return_value=Mock(request=Mock(return_value={"draft": False}))
            ),
            patch.object(release, "build") as build,
            patch.object(release, "publish") as publish,
        ):
            release.main()
            build.assert_not_called()
            publish.assert_not_called()


class CharacterisationUpdaterTests(unittest.TestCase):
    def test_generate_writes_every_reviewed_case_and_main_reports_them(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "nested"
            names = updater.generate(target)
            self.assertEqual(sorted(path.stem for path in target.glob("*.json")), names)
            self.assertEqual(len(names), 5)
            for name in names:
                fresh = (target / f"{name}.json").read_bytes()
                self.assertNotIn(b"\r", fresh)
                committed = (updater.FIXTURES / f"{name}.json").read_bytes()
                self.assertEqual(fresh, committed)
        with (
            patch.object(updater, "generate", return_value=["one"]),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            self.assertEqual(updater.main(), 0)
        self.assertEqual(out.getvalue(), "wrote one.json\n")

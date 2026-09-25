"""Offline replay, quality-gate, package and release transport regressions."""

import argparse
import base64
import contextlib
import importlib
import io
import json
import struct
import sys
import tempfile
import unittest
import urllib.error
import zlib
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
replay = importlib.import_module("replay_autolabels")
release = importlib.import_module("release")
validator = importlib.import_module("validate_package")
complexity = importlib.import_module("check_complexity")


def block(count=100):
    raw = b"".join(
        struct.pack(">H", i * 6) + bytes([5 if i < count // 2 else 30] * 18) for i in range(count)
    )
    return {"start": 0, "count": count, "data": base64.b64encode(zlib.compress(raw)).decode()}


class ReplayTests(unittest.TestCase):
    def test_replay_reports_are_anonymous_and_never_change_source_data(self):
        device = {
            "history": [block()],
            "history_labels": [],
            "entities": {"sensor.synthetic": {"gate": 0, "kind": "move"}},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root / "store.json"
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
                                        {"start": 0, "end": 300, "state": "not_present"},
                                        {"start": 300, "end": 600, "state": "present"},
                                    ]
                                }
                            }
                        }
                    }
                )
            )
            before = store.read_bytes(), labels.read_bytes()
            args = argparse.Namespace(
                store=store,
                labels=labels,
                output=output,
                baseline=replay.PACKAGE / "presence" / "inference.py",
            )
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                reports = replay.run(args)
            self.assertEqual(len(reports), 3)
            self.assertEqual(json.loads(output.read_text()), reports)
            self.assertNotIn("private_device", output.read_text() + stdout.getvalue())
            self.assertEqual(before, (store.read_bytes(), labels.read_bytes()))
            for report in reports:
                self.assertEqual(report["before"], report["after"])
                self.assertEqual(sum(report["after"]["validation"]["matrix"].values()), 20)

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
        malformed = block()
        malformed["count"] += 1
        with self.assertRaisesRegex(ValueError, "block length"):
            replay.decode_rows({"history": [malformed]})
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
                },
            ),
            patch.object(sys, "argv", ["release.py", "--publish"]),
            patch.object(release, "git", return_value="a" * 40),
            patch.object(release, "changed_version", return_value=None),
            patch.object(release, "build") as build,
            patch.object(release, "publish") as publish,
        ):
            release.main()
            build.assert_not_called()
            publish.assert_not_called()

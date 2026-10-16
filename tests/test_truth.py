"""Room truth precedence: own labels, implied labels, member labels, then automatic evidence."""

import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

import room_scenes
import test_rooms as rooms_harness
import test_tuner as harness
from sample_rows import found_of
from tuner_under_test.rooms import (
    application,
    assessment,
    attribution,
    fitting,
    groups,
    jobs,
    learning,
    room_labels,
    truth,
)
from tuner_under_test.rooms import objective as objective_module
from tuner_under_test.rooms.intervals import Timeline
from tuner_under_test.rooms.objective import Objective

AREA = {"name": "Office", "area_id": "office", "kind": "area", "device_ids": ["a", "b"]}
DESK = {"name": "Desk", "area_id": "office", "kind": "zone", "device_ids": ["a"]}
LOOSE = {"name": "Loose", "area_id": None, "kind": "zone", "device_ids": ["a", "b"]}
GROUPS = {"area:office": AREA, "zone:desk": DESK, "zone:loose": LOOSE}


def label(state, source="manual", start=0, end=100, **extra):
    return {"start": start, "end": end, "state": state, "source": source, **extra}


def device(manual=None, automatic=()):
    """Member evidence as ``evidence.read`` builds it: a manual reader and automatic spans."""
    return {"manual": lambda _t: manual, "automatic": Timeline(list(automatic))}


def auto(state, confidence):
    return [(0, 100, (state, "automatic", confidence))]


def members(*states):
    return {f"m{i}": device(state) for i, state in enumerate(states)}


def view(group_id, **labels):
    return {"group_id": group_id, "groups": GROUPS, "room_labels": labels}


def at(group_id, inputs, labels=None, estimate=None):
    context = truth.context(view(group_id, **(labels or {})), group_id, inputs, estimate)
    return truth.room_truth(context, 50)


class PrecedenceTests(unittest.TestCase):
    def test_own_label_beats_member_labels(self):
        inputs = members("present", "present")
        own = {"area:office": [label("not_present")]}
        self.assertEqual(at("area:office", inputs, own), ("not_present", "human", 1.0))
        own = {"zone:desk": [label("present")]}
        self.assertEqual(at("zone:desk", members("not_present"), own), ("present", "human", 1.0))

    def test_zone_present_implies_area_present(self):
        inputs = members("not_present", "not_present")
        zone = {"zone:desk": [label("present")]}
        self.assertEqual(at("area:office", inputs, zone), ("present", "human", 1.0))
        # An empty or unknown zone implies nothing about its area.
        for state in ("not_present", "unknown"):
            with self.subTest(state=state):
                zone = {"zone:desk": [label(state)]}
                self.assertEqual(at("area:office", inputs, zone), ("not_present", "human", 1.0))
        # The area's own label still wins over an implication.
        both = {"zone:desk": [label("present")], "area:office": [label("not_present")]}
        self.assertEqual(at("area:office", inputs, both), ("not_present", "human", 1.0))

    def test_area_empty_implies_zone_empty(self):
        inputs = members("present")
        area = {"area:office": [label("not_present")]}
        self.assertEqual(at("zone:desk", inputs, area), ("not_present", "human", 1.0))
        # An occupied area says nothing about which zone is occupied.
        for state in ("present", "unknown"):
            with self.subTest(state=state):
                area = {"area:office": [label(state)]}
                self.assertEqual(at("zone:desk", members("not_present"), area)[0], "not_present")
                self.assertEqual(at("zone:desk", inputs, area), ("present", "human", 1.0))

    def test_area_less_group_has_no_implications(self):
        inputs = members("not_present", "not_present")
        loose = {"zone:loose": [label("present")]}
        self.assertEqual(at("area:office", inputs, loose), ("not_present", "human", 1.0))
        others = {"area:office": [label("present")], "zone:desk": [label("present")]}
        self.assertEqual(at("zone:loose", inputs, others), ("not_present", "human", 1.0))
        others = {"area:office": [label("not_present")]}
        self.assertEqual(at("zone:loose", members("present"), others), ("present", "human", 1.0))

    def test_member_derivation_matches_old_rule(self):
        cases = [
            (device("present"), device("not_present")),
            (device("not_present"), device("not_present")),
            (device("not_present"), device(automatic=auto("not_present", 0.7))),
            (device(automatic=auto("present", 0.6)), device(automatic=auto("present", 0.9))),
            (device(automatic=auto("present", 0.9)), device("present")),
            (device("unknown"), device("present")),
            (device("unknown"), device("not_present")),
            (device(), device("not_present")),
            (device(), device()),
            (
                device(automatic=auto("not_present", 0.6)),
                device(automatic=auto("not_present", 0.8)),
            ),
            (device("not_present"),),
        ]
        for case in cases:
            inputs = dict(enumerate(case))
            expected = room_scenes.room_label(inputs, 50)
            with self.subTest(case=expected):
                for group_id in GROUPS:
                    self.assertEqual(at(group_id, inputs), expected)
                bare = truth.context(None, None, inputs)
                self.assertEqual(truth.room_truth(bare, 50), expected)

    def test_explicit_unknown_stops_fallback(self):
        sensor = [label("present", "sensor", confidence=0.8)]
        estimate = lambda _t: ("present", "automatic", 0.5)  # noqa: E731
        own = {"area:office": [label("unknown"), *sensor]}
        self.assertIsNone(at("area:office", members("present"), own, estimate))
        zone = {"zone:desk": [label("unknown")], "area:office": sensor}
        self.assertEqual(at("area:office", members(None), zone), ("present", "automatic", 0.8))
        stopped = {"area:office": sensor}
        self.assertIsNone(at("area:office", members("unknown", None), stopped, estimate))
        # Missing member evidence is not a human Unknown, so automatic evidence applies.
        self.assertEqual(
            at("area:office", members(None, "not_present"), stopped),
            ("present", "automatic", 0.8),
        )
        self.assertEqual(
            at("area:office", members(None), {}, estimate), ("present", "automatic", 0.5)
        )

    def test_human_outranks_sensor_and_estimate(self):
        sensor = label("not_present", "sensor", confidence=0.7)
        estimate = lambda _t: ("present", "automatic", 0.9)  # noqa: E731
        labels = {"area:office": [label("present"), sensor]}
        self.assertEqual(at("area:office", members(None), labels, estimate)[1], "human")
        labels = {"area:office": [sensor]}
        result = at("area:office", members(None), labels, estimate)
        self.assertEqual(result, ("not_present", "automatic", 0.7))
        self.assertEqual(
            at("area:office", members("not_present"), labels, estimate),
            ("not_present", "human", 1.0),
        )
        self.assertIsNone(at("area:office", members(None), {}))

    def test_sensor_unknown_falls_through_to_estimate(self):
        labels = {"area:office": [label("unknown", "sensor", confidence=0.9)]}
        estimate = lambda _t: ("not_present", "automatic", 0.4)  # noqa: E731
        self.assertEqual(at("area:office", members(None), labels, estimate)[2], 0.4)


class ReaderTests(unittest.TestCase):
    def test_reader_filters_sources_and_keeps_last_label(self):
        labels = [
            label("present", start=0, end=10),
            label("not_present", start=5, end=20),
            label("present", "sensor", start=0, end=30, confidence=0.6),
            label("not_present", "sensor", start=40, end=50),
        ]
        manual = truth.reader(labels, truth.MANUAL)
        sensor = truth.reader(labels, truth.SENSOR)
        self.assertEqual(manual(2), ("present", "human", 1.0))
        self.assertEqual(manual(7), ("not_present", "human", 1.0))
        self.assertIsNone(manual(20))
        self.assertIsNone(manual(-1))
        self.assertEqual(sensor(25), ("present", "automatic", 0.6))
        self.assertEqual(sensor(45), ("not_present", "automatic", 1.0))
        self.assertIsNone(truth.reader([], truth.MANUAL)(0))

    def test_context_collects_label_edges_for_spans(self):
        labels = {
            "area:office": [label("present", start=3, end=9)],
            "zone:desk": [label("present", start=11, end=12)],
            "zone:loose": [label("present", start=50, end=60)],
        }
        context = truth.context(view("area:office", **labels), "area:office", {})
        self.assertEqual(context["boundaries"], {3, 9, 11, 12})
        self.assertEqual(truth.context(None, None, {})["boundaries"], set())

    def test_related_groups(self):
        self.assertEqual(truth.related(GROUPS, "area:office"), ("area:office", None, ["zone:desk"]))
        self.assertEqual(truth.related(GROUPS, "zone:desk"), ("zone:desk", "area:office", []))
        self.assertEqual(truth.related(GROUPS, "zone:loose"), ("zone:loose", None, []))
        self.assertEqual(truth.related(GROUPS, "zone:gone"), ("zone:gone", None, []))


def office_member(key):
    return rooms_harness.member(key, [40] * 100 + [0] * 100, rooms_harness.labels())


class AssessmentTests(unittest.TestCase):
    def office_view(self, **labels):
        return view("area:office", **labels)

    def test_no_room_labels_keeps_assessment_identical(self):
        data = {"a": office_member("a"), "b": office_member("b")}
        old = assessment.calculate(data, 0, 1200)
        self.assertEqual(assessment.calculate(data, 0, 1200, self.office_view()), old)
        self.assertEqual(assessment.calculate(data, 0, 1200, None), old)

    def test_room_label_splits_spans_and_overrides_members(self):
        data = {"a": office_member("a"), "b": office_member("b")}
        room = self.office_view(**{"area:office": [label("not_present", start=0, end=201)]})
        report = assessment.calculate(data, 0, 1200, room)
        human = report["room"]["outcomes"]["human"]
        self.assertAlmostEqual(human["present"]["seconds"], 399)
        # Readings held until the next; the recording's last holds half a sample (to 1197).
        self.assertAlmostEqual(human["not_present"]["seconds"], 201 + 600 - 3)

    def test_objective_and_fit_use_room_truth(self):
        data = {"a": office_member("a"), "b": office_member("b")}
        plain = Objective(data, 0, 1200)
        same = Objective(data, 0, 1200, self.office_view())
        self.assertEqual(plain.totals, same.totals)
        self.assertEqual(plain.preferences, same.preferences)
        room = self.office_view(**{"area:office": [label("not_present", start=0, end=201)]})
        labelled = Objective(data, 0, 1200, room)
        self.assertAlmostEqual(labelled.totals["human", "present"], 399)
        result = fitting.fit(data, 0, 1200, room)
        self.assertAlmostEqual(
            result["before"]["room"]["outcomes"]["human"]["present"]["seconds"], 399
        )
        self.assertEqual(fitting.fit(data, 0, 1200, self.office_view()), fitting.fit(data, 0, 1200))
        compared = application.compare(data, result["thresholds"], 0, 1200, room)
        self.assertAlmostEqual(compared["live"]["outcomes"]["human"]["present"]["seconds"], 399)


class RuntimeTruthTests(unittest.IsolatedAsyncioTestCase):
    setUp = rooms_harness.GroupTests.setUp
    asyncTearDown = rooms_harness.GroupTests.asyncTearDown
    office = rooms_harness.GroupTests.office

    def test_signature_unchanged_without_room_labels(self):
        self.office()
        zone = groups.configure(self.runtime, None, "Desk", "office", ["a"])["id"]
        for group_id in ("area:office", zone):
            with self.subTest(group_id=group_id):
                self.assertNotIn("room_labels", jobs._context(self.runtime, group_id)[1])
        self.runtime.data["room_label_revision"] = {"area:office": 0, zone: 0}
        self.assertNotIn("room_labels", jobs._context(self.runtime, "area:office")[1])

    def test_signature_tracks_group_parent_and_zones(self):
        self.office()
        zone = groups.configure(self.runtime, None, "Desk", "office", ["a"])["id"]
        loose = groups.configure(self.runtime, None, "Loose", None, ["a", "b"])["id"]
        room_labels.label_room_range(self.runtime, zone, 10, 20, "present")
        room_labels.label_room_range(self.runtime, loose, 10, 20, "present")
        signature = jobs._context(self.runtime, "area:office")[1]
        self.assertEqual(signature["room_labels"], {zone: 1})
        room_labels.label_room_range(self.runtime, "area:office", 10, 20, "present")
        self.assertEqual(
            jobs._context(self.runtime, zone)[1]["room_labels"], {zone: 1, "area:office": 1}
        )
        self.assertEqual(jobs._context(self.runtime, loose)[1]["room_labels"], {loose: 1})

    def test_view_copies_relevant_labels(self):
        self.office()
        room_labels.label_room_range(self.runtime, "area:office", 0, 201, "not_present")
        room = truth.view(self.runtime, "area:office")
        self.runtime.data["room_labels"]["area:office"][0]["state"] = "present"
        self.assertEqual(room["group_id"], "area:office")
        self.assertEqual(room["room_labels"]["area:office"][0]["state"], "not_present")

    async def test_assessment_job_uses_room_labels(self):
        self.office()
        room_labels.label_room_range(self.runtime, "area:office", 0, 201, "not_present")
        with patch.object(jobs.time, "time", return_value=1200):
            jobs.start(self.runtime, "area:office", 1)
            await self.runtime._room_jobs["area:office"]
        report = jobs.summary(self.runtime, "area:office")
        self.assertEqual(report["state"], "ready")
        self.assertAlmostEqual(report["room"]["outcomes"]["human"]["present"]["seconds"], 399)

    async def test_learning_and_automatic_apply_use_room_labels(self):
        self.office()
        room_labels.label_room_range(self.runtime, "area:office", 0, 201, "not_present")
        with (
            patch.object(learning.recovery, "prepare_learning", AsyncMock()),
            patch.object(learning.time, "time", return_value=1200),
        ):
            await learning.start_device(self.runtime, "a", "user", "area:office")
        learned = self.runtime.data["room_learning"]["area:office"]["slots"]["user"]
        before = learned["before"]["room"]["outcomes"]["human"]["present"]
        self.assertAlmostEqual(before["seconds"], 399)
        self.runtime.data["learning_schedule"] = {"enabled": True, "auto_apply": True}
        report = {}
        with patch.object(application, "time", return_value=1200):
            await application.automatic(self.runtime, learned, report, "automatic")
        live = report["assessment"]["patterns"]["live"]["outcomes"]["human"]["present"]
        self.assertAlmostEqual(live["seconds"], 399)


class EstimateTests(unittest.TestCase):
    def test_noisy_or_present(self):
        result = truth.estimate([("present", 0.6), ("present", 0.5), ("not_present", 0.9), None])
        self.assertEqual(result[:2], ("present", "automatic"))
        self.assertAlmostEqual(result[2], 0.8)

    def test_empty_needs_all_members(self):
        result = truth.estimate([("not_present", 0.9), ("not_present", 0.7)])
        self.assertEqual(result, ("not_present", "automatic", 0.7))
        self.assertIsNone(truth.estimate([("not_present", 0.9), None]))
        self.assertIsNone(truth.estimate([]))

    def test_mixed_is_none(self):
        self.assertIsNone(truth.estimate([None, None]))
        self.assertIsNone(truth.estimate([("not_present", 0.9), None, ("not_present", 0.9)]))

    def test_below_floor_is_none(self):
        self.assertIsNone(truth.estimate([("present", 0.54)]))
        self.assertIsNone(truth.estimate([("not_present", 0.9), ("not_present", 0.5)]))
        self.assertEqual(truth.estimate([("present", 0.55)])[2], 0.55)

    def test_reader_uses_member_guesses_and_for_view_wires_it(self):
        inputs = {
            "m0": device(automatic=auto("present", 0.6)),
            "m1": device(automatic=auto("present", 0.6)),
        }
        self.assertEqual(truth.estimate_reader(inputs)(50)[0], "present")
        self.assertIsNone(truth.estimate_reader(inputs)(150))
        self.assertIsNotNone(truth.for_view(view("area:office"), inputs)["estimate"])
        self.assertIsNone(truth.estimate_reader({})(50))

    def test_every_members_guess_counts(self):
        inputs = {
            "m0": device(automatic=auto("present", 0.6)),
            "m1": device(automatic=auto("present", 0.5)),
        }
        self.assertAlmostEqual(truth.estimate_reader(inputs)(50)[2], 0.8)
        empty = {"m0": device(automatic=auto("not_present", 0.9)), "m1": device()}
        self.assertIsNone(truth.estimate_reader(empty)(50))

    def test_estimate_is_below_sensor_and_never_human(self):
        inputs = {"m0": device(automatic=auto("present", 0.9))}
        labels = {"area:office": [label("present", "sensor", confidence=0.7)]}
        context = truth.for_view(view("area:office", **labels), inputs)
        self.assertEqual(truth.room_truth(context, 50)[1], "automatic")


def guess_records(codes, confidence=100, step=6, first=0):
    """Rows carrying a recorded guess per sample (``code``: 1 present, 2 not present)."""
    return [(first + i * step, [0] * 18 + [code, confidence]) for i, code in enumerate(codes)]


class FeedbackTests(unittest.TestCase):
    """The server finds the stretch: the latest run of one confident recorded estimate."""

    def runtime(self, records, devices=("a", "b")):
        return self.member_runtime(dict.fromkeys(devices, records))

    def run_feedback(self, records, correct, now=120.0, devices=("a", "b"), group="area:office"):
        office = {"name": "Office", "area_id": "office", "kind": "area", "device_ids": ["a", "b"]}
        with (
            patch.object(groups, "definitions", return_value=(None, None, {"area:office": office})),
            patch.object(room_labels.time, "time", return_value=now),
            patch.object(room_labels.membership, "labels_changed"),
        ):
            return room_labels.room_estimate_feedback(
                self.runtime(records, devices), group, correct
            )

    def test_feedback_labels_the_whole_recorded_stretch(self):
        right = self.run_feedback(guess_records([1] * 20), True)
        self.assertEqual(
            right["room_labels"],
            [{"start": 0, "end": 120, "state": "present", "source": "manual"}],
        )
        wrong = self.run_feedback(guess_records([1] * 20), False)
        self.assertEqual(wrong["room_labels"][0]["state"], "not_present")
        wrong = self.run_feedback(guess_records([2] * 20), False)
        self.assertEqual(wrong["room_labels"][0]["state"], "present")

    def test_feedback_rejects_missing_stale_or_unsure_estimate(self):
        for records, kwargs in (
            (guess_records([0] * 20), {}),
            (guess_records([1] * 20, confidence=40), {}),
            (guess_records([1] * 20), {"now": 200.0}),
            ([], {}),
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.run_feedback(records, True, **kwargs)

    def test_a_brief_recorded_flip_shortens_the_stretch_instead_of_rejecting(self):
        flipped = guess_records([1] * 8 + [2] + [1] * 11)
        for _ in range(2):  # Never stuck: the same request succeeds again.
            result = self.run_feedback(flipped, True)
            # A recorded change at 54 holds until the explicit coverage end.
            self.assertEqual(result["room_labels"][0]["start"], 54)
            self.assertEqual(result["room_labels"][0]["end"], 120)

    def test_a_recording_gap_ends_the_stretch(self):
        records = guess_records([1] * 5) + guess_records([1] * 5, first=90)
        result = self.run_feedback(records, True, now=120.0)
        self.assertEqual(result["room_labels"][0]["start"], 90)

    def test_the_stretch_is_capped(self):
        count = int(room_labels.FEEDBACK_MAX_SECONDS / 6) + 50
        records = guess_records([1] * count)
        now = records[-1][0] + 1.0
        result = self.run_feedback(records, True, now=now)
        start = result["room_labels"][0]["start"]
        self.assertGreaterEqual(start, now - room_labels.FEEDBACK_MAX_SECONDS)
        self.assertLess(start, now - room_labels.FEEDBACK_MAX_SECONDS + 7)

    def test_no_estimate_when_the_latest_samples_carry_no_confident_guess(self):
        """The stretch is anchored at the newest sample: a tail with no guess, or a guess
        below the floor, means there is no current estimate to answer."""
        tails = {
            "no guess": guess_records([1] * 10 + [0] * 10),
            "below floor": guess_records([1] * 10)
            + guess_records([1] * 10, confidence=40, first=60),
        }
        # Recent enough, but the newest sample (114) is two unguessed samples past the last
        # confident cell (ends 105): only the anchor catches this one.
        tails["short no-guess tail"] = guess_records([1] * 18 + [0] * 2)
        for name, records in tails.items():
            with self.subTest(tail=name):
                with patch.object(room_labels.time, "time", return_value=115.0):
                    self.assertIsNone(room_labels.current_estimate(self.runtime(records), ["a"]))
                with self.assertRaises(ValueError):
                    self.run_feedback(records, True)

    def member_runtime(self, records, ends=None):
        """Synthetic native traces; each row holds six seconds, gaps explicitly end sessions."""
        from tuner_under_test.history import dayfile, daystore, guesses

        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = daystore.DayStore(Path(folder.name))
        for key, rows in records.items():
            previous = None
            for timestamp, row in rows:
                if previous is not None and timestamp - previous > guesses.GAP:
                    store.append(key, dayfile.End(previous + 6))
                store.append(key, dayfile.Change(timestamp, dict(enumerate(row[:18]))))
                state = {1: "present", 2: "not_present"}.get(row[18])
                store.append(
                    key, dayfile.Guess(timestamp, state, guesses.level(row[19]), guesses.UNKNOWN)
                )
                previous = timestamp
            if previous is not None:
                store.append(key, dayfile.End((ends or {}).get(key, previous + 6)))

        return types.SimpleNamespace(
            data={
                "devices": {key: {"key": key} for key in records},
                "room_labels": {},
            },
            _days=store,
            _history_runtime={},
            _iter_history_samples=lambda *args, **kwargs: iter(()),
            _schedule_save=lambda: None,
        )

    def test_the_estimate_ends_at_the_last_estimated_sample(self):
        """A no-guess sample after the last guess is not estimated time: once every member's
        newest sample is unguessed there is no estimate, and the end never reaches it."""
        stopped = guess_records([1] * 19 + [0])  # last guess 108, unguessed 114
        cases = {
            "one member stopped": self.runtime(stopped, devices=("a",)),
            "both stopped": self.member_runtime(
                {"a": stopped, "b": guess_records([1] * 18 + [0] * 2, first=3)}
            ),
            # a's newest (102) is a guess and recent, but b has recorded unguessed rows 9 s
            # past it: beyond the one-sample slack, so the estimate is not current.
            "a lagging": self.member_runtime(
                {"a": guess_records([1] * 18), "b": guess_records([1] * 17 + [0] * 2, first=3)}
            ),
        }
        with patch.object(room_labels.time, "time", return_value=115.0):
            for name, runtime in cases.items():
                with self.subTest(case=name):
                    members = list(runtime.data["devices"])
                    self.assertIsNone(room_labels.current_estimate(runtime, members))
            # b still guesses (newest 111): the estimate holds to b's last estimated cell.
            guessing = self.member_runtime(
                {"a": stopped, "b": guess_records([1] * 19, first=3)}, ends={"a": 114, "b": 114}
            )
            estimate = room_labels.current_estimate(guessing, ["a", "b"])
        self.assertEqual((estimate["state"], estimate["end"]), ("present", 114.0))
        # b's explicit end (114) anchors it; a's present coverage runs until 118,
        # then unguessed coverage through 120. Both agree throughout the anchored stretch.
        overlap = self.member_runtime(
            {"a": guess_records([1] * 19 + [0], first=4), "b": guess_records([1] * 20)},
            ends={"a": 120, "b": 114},
        )
        with patch.object(room_labels.time, "time", return_value=120.0):
            estimate = room_labels.current_estimate(overlap, ["a", "b"])
        self.assertEqual((round(estimate["confidence"], 6), estimate["end"]), (0.9975, 114.0))
        with self.assertRaises(ValueError):
            self.run_feedback(stopped, True, now=115.0)

    def test_feedback_rejects_unknown_group(self):
        with self.assertRaises(ValueError):
            self.run_feedback(guess_records([1] * 20), True, group="zone:gone")

    def test_feedback_treats_a_missing_member_as_having_no_guess(self):
        self.run_feedback(guess_records([2] * 20), True)  # Both members agree: allowed.
        with self.assertRaises(ValueError):
            self.run_feedback(guess_records([2] * 20), True, devices=("a",))
        # Present needs two members agreeing, so a missing one blocks it too.
        with self.assertRaises(ValueError):
            self.run_feedback(guess_records([1] * 20), True, devices=("a",))

    def test_current_estimate_reports_state_confidence_and_stretch(self):
        runtime = self.runtime(guess_records([1] * 8 + [2] + [1] * 11))
        with patch.object(room_labels.time, "time", return_value=120.0):
            estimate = room_labels.current_estimate(runtime, ["a", "b"])
        self.assertEqual(
            {key: round(value, 6) for key, value in estimate.items() if key != "state"},
            {"confidence": 0.9975, "start": 54, "end": 120},
        )
        self.assertEqual(estimate["state"], "present")
        with patch.object(room_labels.time, "time", return_value=500.0):
            self.assertIsNone(room_labels.current_estimate(runtime, ["a", "b"]))


def guessed_member(key, values, guesses, labels=()):
    """A member whose rows carry recorded guesses ``(code, confidence)`` per sample."""
    records = [
        (i * 6, [value] + [255] * 17 + list(guess))
        for i, (value, guess) in enumerate(zip(values, guesses, strict=True))
    ]
    device = {"history_labels": list(labels), "training_state": "unknown"}
    view = harness.stub_view(key, device, records)
    return {"view": view, "thresholds": {"g0_move": 20}, "timing": {}}


class TrainingObjectiveTests(unittest.TestCase):
    """A radar's own automatic guesses train it too, down-weighted by confidence."""

    def preferences(self, data):
        seen = []

        def capture(key, human, automatic, value):
            seen.append(automatic)
            return {"preferred_threshold": 20}

        with patch.object(objective_module, "gate_preference", capture):
            Objective(data, 0, 1200, view("area:office"))
        return seen

    def test_own_guess_gives_an_automatic_preference_with_its_confidence(self):
        quiet = [{"start": 0, "end": 300, "state": "not_present"}]
        values = [5] * 50 + [70] * 50
        own = [(0, 0)] * 50 + [(1, 99)] * 50  # b guesses Present after its Empty labels ...
        data = {
            "a": guessed_member("a", [5] * 100, own),  # ... and a agrees: a room needs two.
            "b": guessed_member("b", values, own, quiet),
        }
        automatic = self.preferences(data)[1]  # Members are fitted in order: a, then b.
        # 50 samples of b's present guess, by how long the readings held (six-second units;
        # the recording's last reading holds half a sample).
        self.assertAlmostEqual(sum(row[4] for row in automatic["present"]), 49.5)
        # Recorded confidence is kept in steps of GUESS_CONFIDENCE_STEP (99 % is stored as
        # 100 %), and counts as calibrated (0.95), the room label of two agreeing members.
        self.assertEqual({row[3] for row in automatic["present"]}, {0.95})


class WeightedTargetTests(unittest.TestCase):
    def target(self, *labels):
        truths = {f"g{i}": (lambda _t, x=x: x) for i, x in enumerate(labels)}
        found = found_of("r", [], lambda _t: None, truths)
        return attribution.weighted_member_target("r", lambda _t: None, truths, found=found)(0)

    def test_mixed_human_and_automatic_empty_is_automatic_at_minimum_confidence(self):
        human, auto_ = ("not_present", "human", 1.0), ("not_present", "automatic", 0.7)
        self.assertEqual(self.target(human, auto_), ("not_present", "automatic", 0.7))
        self.assertEqual(self.target(human, human), ("not_present", "human", 1.0))
        estimate_ = ("not_present", "estimate", 0.6)
        self.assertEqual(self.target(estimate_, estimate_), ("not_present", "estimate", 0.6))
        self.assertEqual(self.target(auto_, estimate_), ("not_present", "automatic", 0.6))
        self.assertIsNone(self.target(human, ("present", "human", 1.0)))


def sensor_room(state="present", confidence=0.95, end=100):
    return view(
        "area:office",
        **{"area:office": [label(state, "sensor", end=end, confidence=confidence)]},
    )


def guessing(state, confidence):
    return {key: device(automatic=auto(state, confidence)) for key in ("a", "b")}


class SensorLevelTests(unittest.TestCase):
    """Room sensor labels sit between member-derived human truth and members' automatic
    guesses, for assessment, fitting and learning alike (final review I1)."""

    def assessed(self, room, inputs):
        return truth.room_truth(truth.for_view(room, inputs), 50)

    def learned(self, room, inputs):
        """The learner's context: human-only member readers, the estimate from the guesses."""
        unguessed = {key: device(item["manual"](50)) for key, item in inputs.items()}
        context = truth.context(room, "area:office", unguessed, truth.estimate_reader(inputs))
        return truth.room_truth(context, 50)

    def test_camera_present_beats_member_empty_guesses_everywhere(self):
        room, inputs = sensor_room(), guessing("not_present", 0.6)
        self.assertEqual(self.assessed(room, inputs), ("present", "automatic", 0.95))
        self.assertEqual(self.learned(room, inputs), ("present", "automatic", 0.95))

    def test_camera_empty_beats_a_member_present_guess(self):
        room = sensor_room("not_present", 0.9)
        inputs = {"a": device(automatic=auto("present", 0.8)), "b": device()}
        self.assertEqual(self.assessed(room, inputs), ("not_present", "automatic", 0.9))

    def test_member_human_labels_still_beat_the_sensor(self):
        inputs = {"a": device("not_present"), "b": device("not_present")}
        self.assertEqual(self.assessed(sensor_room(), inputs), ("not_present", "human", 1.0))
        inputs = {"a": device("present"), "b": device(automatic=auto("not_present", 0.9))}
        self.assertEqual(self.assessed(sensor_room("not_present"), inputs)[1], "human")

    def test_without_sensor_labels_member_guesses_decide_as_before(self):
        for inputs in (
            guessing("not_present", 0.6),
            {"a": device(automatic=auto("present", 0.6)), "b": device()},
            {"a": device("unknown"), "b": device(automatic=auto("present", 0.7))},
        ):
            with self.subTest(inputs=inputs):
                expected = room_scenes.room_label(inputs, 50)
                self.assertEqual(self.assessed(view("area:office"), inputs), expected)
                self.assertEqual(self.assessed(sensor_room(end=40), inputs), expected)

    def test_objective_scores_the_sensor_over_member_guesses(self):
        empty = [(2, 60)] * 200  # Both radars record a 0.6 Empty guess at every sample.
        data = {
            "a": guessed_member("a", [40] * 100 + [0] * 100, empty),
            "b": guessed_member("b", [0] * 200, empty),
        }
        room = sensor_room("present", 0.95, end=600)
        totals = Objective(data, 0, 1200, room).totals
        # The sensor stretch counts as occupied as a human room label over it would, weighted
        # by its confidence; before, the members' Empty guesses pre-empted it (0 s).
        human = view("area:office", **{"area:office": [label("present", end=600)]})
        occupied = Objective(data, 0, 1200, human).totals["human", "present"]
        self.assertGreater(occupied, 0)
        self.assertAlmostEqual(totals["automatic", "present"], 0.95 * occupied)
        plain = Objective(data, 0, 1200, view("area:office")).totals
        self.assertEqual(plain["automatic", "present"], 0)

    def test_one_group_set_fit_matches_fit_with_sensor_labels(self):
        def data():
            empty = [(2, 60)] * 200
            return {
                "a": guessed_member("a", [40] * 100 + [0] * 100, empty),
                "b": guessed_member("b", [30] * 50 + [0] * 150, empty),
            }

        room = sensor_room("present", 0.95, end=600)
        expected = fitting.fit(data(), 0, 1200, room)
        result = fitting.fit_set({"area:office": data()}, 0, 1200, {"area:office": room})
        result.pop("groups")
        self.assertEqual(result, expected)
        self.assertGreater(
            expected["before"]["room"]["outcomes"]["automatic"]["present"]["seconds"], 0
        )


class SensorFloorTests(unittest.TestCase):
    """Room sensor labels below MIN_AUTO_CONFIDENCE are no evidence, like device sources
    and recorded guesses (final review M1)."""

    def test_reader_drops_sensor_labels_below_the_floor(self):
        floor = attribution.MIN_AUTO_CONFIDENCE
        labels = [
            label("present", "sensor", start=0, end=10, confidence=floor - 0.01),
            label("not_present", "sensor", start=10, end=20, confidence=floor),
        ]
        sensor = truth.reader(labels, truth.SENSOR)
        self.assertIsNone(sensor(5))
        self.assertEqual(sensor(15), ("not_present", "automatic", floor))

    def test_a_weak_sensor_label_falls_through_to_the_estimate(self):
        estimate = lambda _t: ("not_present", "automatic", 0.7)  # noqa: E731
        weak = {"area:office": [label("present", "sensor", confidence=0.5)]}
        self.assertEqual(at("area:office", members(None), weak, estimate), estimate(0))
        self.assertIsNone(at("area:office", members(None), weak))

    def test_objective_ignores_a_weak_sensor_label(self):
        data = {"a": guessed_member("a", [0] * 200, [(0, 0)] * 200)}
        room = sensor_room("present", 0.5, end=600)
        self.assertEqual(Objective(data, 0, 1200, room).totals["automatic", "present"], 0)

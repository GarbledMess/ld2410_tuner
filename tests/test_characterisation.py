"""Golden-file baseline of room, learning and reference behaviour.

Fixtures are regenerated only by ``tools/update_characterisation.py``; a test never writes them.
"""

import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import test_joint_learning as joint
import test_references as references
import test_tuner as harness
from test_rooms import labels, member
from tuner_under_test.calibration import settings
from tuner_under_test.rooms import assessment, fitting, learning, roles

FIXTURES = Path(__file__).parent / "fixtures" / "characterisation"
VOLATILE = {"started_at", "finished_at", "id", "result_id"}
BASE = 1_800_000_000


def normalise(value, drop=()):
    """Round floats to 9 places and drop volatile keys so goldens are deterministic."""
    if isinstance(value, float):
        return round(value, 9)
    if isinstance(value, dict):
        return {k: normalise(v, drop) for k, v in value.items() if k not in drop}
    if isinstance(value, (list, tuple)):
        return [normalise(v, drop) for v in value]
    return value


def canonical(value, drop=()):
    return json.dumps(normalise(value, drop), sort_keys=True, indent=1) + "\n"


def golden(name, value, drop=()):
    path = FIXTURES / f"{name}.json"
    if not path.exists():
        raise AssertionError(f"missing fixture {path}; run tools/update_characterisation.py")
    assert canonical(value, drop) == path.read_text(encoding="utf-8")


def room_assessment_two_members():
    members = {
        "a": member("a", [40] * 100 + [0] * 100, labels()),
        "b": member("b", [3] * 200, labels()),
    }
    return assessment.calculate(members, 0, 1200)


def joint_fit_examples(automatic_weight=settings.DEFAULT_AUTOMATIC_WEIGHT):
    inputs = {
        "strong_and_weak": ([70] * 100 + [10] * 100, [3] * 200, 20),
        "both_strong": ([70] * 100 + [10] * 100, [50] * 100 + [5] * 100, 100),
        "handoff": ([70] * 50 + [3] * 150, [3] * 50 + [60] * 50 + [30] * 100, 20),
        "different_timing": ([70] * 100 + [10] * 100, [50] * 100 + [5] * 100, 20),
    }
    timing = {
        "different_timing": (
            {"timeout": 1, "on_delay": 0.5, "off_delay": 1},
            {"timeout": 8, "on_delay": 2, "off_delay": 3},
        )
    }
    out = {}
    for name, (a, b, current) in inputs.items():
        data = joint.example(a, b, current)
        for key, value in zip(("a", "b"), timing.get(name, ({}, {})), strict=True):
            data[key]["timing"] = value
        result = fitting.fit(data, 0, 1200, automatic_weight=automatic_weight)
        out[name] = {
            "thresholds": result["thresholds"],
            "cost_before": result["cost_before"],
            "cost_after": result["cost_after"],
            "room": result["after"]["room"],
        }
    return out


def device_fit(automatic_weight=settings.DEFAULT_AUTOMATIC_WEIGHT):
    rows = harness.row_samples({"g0_move": 40}, {"g0_move": 5})
    return harness.fit(rows, ["g0_move"], automatic_weight=automatic_weight)


def overnight_joint_attempt():
    from tuner_under_test.runtime import schedule

    case = joint.JointRuntimeTests()
    case._setupAsyncioRunner()  # Its cleanup dispatcher requires an initialized runner.
    try:
        case.setUp()
        case.office()
        case.runtime.data["learning_schedule"] = {"enabled": True, "auto_apply": False}
        case.runtime.async_save = AsyncMock()
        with (
            patch.object(learning.recovery, "prepare_learning", AsyncMock()),
            patch.object(learning.time, "time", return_value=1200),
        ):
            asyncio.run(schedule._run(case.runtime, "2026-10-07"))
        return {
            key: item["nightly_learning"]
            for key, item in sorted(case.runtime.data["devices"].items())
        }
    finally:
        try:
            case.doCleanups()
        finally:
            case._tearDownAsyncioRunner()


def reference_profile():
    case = references.ReferenceRuntimeTests()
    case._setupAsyncioRunner()
    try:
        case.setUp()
        for i in range(40):
            case.sample(BASE + i * 6, 70 if i < 20 else 8)
        with patch("time.time", return_value=BASE + 1000):

            async def label():
                await case.runtime.label_history_range("a", BASE, BASE + 120, "present")
                await case.runtime.label_history_range("a", BASE + 120, BASE + 240, "not_present")
                await harness.settled(case.runtime)

            case._asyncioRunner.run(label())
            references.training.rebuild_human(case.runtime, case.device)
            profile = case.device["reference_profile"]
            summary = references.refs.summary(profile, BASE + 1000)
        periods = [
            {k: p[k] for k in ("source", "state", "start", "end", "count", "confidence")}
            for p in profile["periods"]
        ]
        return {"summary": summary, "periods": periods}
    finally:
        try:
            case.doCleanups()
        finally:
            case._tearDownAsyncioRunner()


# name -> (builder, keys dropped as volatile); used by tools/update_characterisation.py.
CASES = {
    "room_assessment_two_members": (room_assessment_two_members, ()),
    "joint_fit_examples": (joint_fit_examples, ()),
    "device_fit": (device_fit, ()),
    "overnight_joint_attempt": (overnight_joint_attempt, tuple(VOLATILE)),
    "reference_profile": (reference_profile, ()),
}


def check(name):
    builder, drop = CASES[name]
    golden(name, builder(), drop)


class CharacterisationTests(unittest.TestCase):
    def test_room_assessment_two_members(self):
        """Pin the full report and the additive role-field compatibility contract."""
        report = room_assessment_two_members()
        golden("room_assessment_two_members", report)
        self.assertTrue(all(set(roles.FIELDS) <= set(d) for d in report["devices"].values()))
        expected = json.loads((FIXTURES / "room_assessment_two_members.json").read_text())
        for value in (report, expected):
            for device in value["devices"].values():
                for name in roles.FIELDS:
                    del device[name]
        self.assertEqual(canonical(report), canonical(expected))

    def test_joint_fit_examples(self):
        check("joint_fit_examples")

    def test_device_fit(self):
        check("device_fit")

    def test_overnight_joint_attempt(self):
        check("overnight_joint_attempt")

    def test_reference_profile(self):
        check("reference_profile")

    def test_missing_fixture_names_the_generator(self):
        with self.assertRaisesRegex(AssertionError, "update_characterisation"):
            golden("does_not_exist", {})

    def test_normalise_rounds_floats_and_drops_volatile_keys(self):
        value = {"x": 1 / 3, "id": "k", "n": [(0.1234567891, {"finished_at": 5, "ok": 2})]}
        self.assertEqual(
            normalise(value, VOLATILE), {"x": 0.333333333, "n": [[0.123456789, {"ok": 2}]]}
        )

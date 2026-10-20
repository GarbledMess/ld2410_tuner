"""Device adaptive policy behavior."""

import unittest

import pytest
import test_automatic_weight
import test_tuner  # noqa: F401
from test_tuner import fit


def test_adaptive_prior_preserves_raw_admission_floor():
    automatic = [
        (i * 6, {"g0_move": 40 if state == "present" else 10}, state, 0.55)
        for state, offset in (("present", 0), ("not_present", 1000))
        for i in range(offset, offset + 60)
    ]
    result = fit([], ["g0_move"], automatic=automatic, automatic_weight=0.5)
    assert result["automatic_evidence"]["mean_confidence"]["present"] == pytest.approx(0.55 * 0.75)
    assert result["adaptive_policy"]["selected"] == "corrected_positive"
    assert result["proposals"]


def test_earlier_snapshot_excludes_future_samples_and_labels():
    from types import SimpleNamespace

    from tuner_under_test.calibration.adaptive import earlier_view

    samples = [(i, bytes([10] * 18)) for i in range(10)]
    labels = [
        {"start": 0, "end": 10, "state": "present"},
        {"start": 20, "end": 30, "state": "not_present"},
    ]
    device = {
        "history_labels": labels,
        "training_state": "not_present",
        "training_label_start": 20,
        "training_expires_at": 30,
    }
    view = SimpleNamespace(
        data={"devices": {"arbitrary": device}, "room_snapshot": None},
    )
    test_tuner.record_rows(view, "arbitrary", samples)
    earlier = earlier_view(view, 5)
    from tuner_under_test.history import spans

    cells = list(spans.events(earlier, "arbitrary"))
    assert sum(cell.end - cell.start for cell in cells) == 5
    assert all(cell.end <= 5 for cell in cells)
    assert earlier.data["devices"]["arbitrary"]["history_labels"] == [dict(labels[0], end=5)]
    assert earlier.data["devices"]["arbitrary"]["training_state"] == "unknown"
    assert device["history_labels"] == labels


def _human_scene():
    return [
        (
            block * 1000 + i * 6,
            {"g0_move": 40 if block % 2 == 0 else 10},
            "present" if block % 2 == 0 else "not_present",
        )
        for block in range(6)
        for i in range(60)
    ]


def test_chronological_candidates_use_original_held_duration():

    rows = _human_scene()
    result = fit(
        rows,
        ["g0_move"],
        automatic_weight=0.5,
    )
    audit = result["adaptive_policy"]
    assert audit["selected"] == "reference"
    assert audit["validated"]
    assert audit["reason"] == "held_duration_tie"
    assert audit["candidates"]["reference"]["score"]["present_seconds"] == 360
    assert audit["candidates"]["corrected_positive"]["score"]["empty_seconds"] == 360


def test_held_labels_cannot_change_earlier_candidate_thresholds():

    rows = _human_scene()
    changed = rows[:240] + [(ts, {"g0_move": 1}, state) for ts, _, state in rows[240:]]
    first = fit(
        rows,
        ["g0_move"],
        automatic_weight=0.5,
    )
    second = fit(
        changed,
        ["g0_move"],
        automatic_weight=0.5,
    )
    for name in ("reference", "corrected_positive"):
        assert (
            first["adaptive_policy"]["candidates"][name]["thresholds"]
            == second["adaptive_policy"]["candidates"][name]["thresholds"]
        )


def test_empty_episode_scoring_keeps_final_observed_sample():
    from types import SimpleNamespace

    from tuner_under_test.calibration.event_fitting import assess

    held = [(i * 6, {"g0_move": 40}, "not_present") for i in range(60)]
    view = SimpleNamespace(data={"devices": {"d": {}}, "room_snapshot": None})
    test_tuner.record_samples(view, "d", held)
    report = assess(view, "d", {"g0_move": 10})["human"]["duration"]
    assert report["false_positive_seconds"] == 360
    assert report["empty_seconds"] == 360


class RuntimeAdaptiveTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_automatic_weight.DeviceLearnTests.setUp
    asyncTearDown = test_automatic_weight.DeviceLearnTests.asyncTearDown
    configuration = test_automatic_weight.DeviceLearnTests.configuration
    labelled = test_automatic_weight.DeviceLearnTests.labelled

    async def test_history_replacement_during_learning_rejects_installation(self):
        from unittest.mock import AsyncMock, patch

        from tuner_under_test.calibration import service

        entities, current = self.labelled()

        async def executor(request):
            self.runtime._days.remove_device("a")  # The radar's history is cleared meanwhile.

        self.worker_transport.before = executor
        with (
            patch.object(service, "read_timing", return_value={}),
            patch.object(
                service.recovery, "prepare_learning", AsyncMock(return_value=(entities, current))
            ),
            self.assertRaisesRegex(service.LearningEvidenceChanged, "history"),
        ):
            await service._learn_once(self.runtime, "a")

    async def test_history_append_during_learning_allows_installation(self):
        from unittest.mock import AsyncMock, patch

        from tuner_under_test.calibration import service

        entities, current = self.labelled()

        async def executor(request):
            from tuner_under_test.history import dayfile

            self.runtime._days.append("a", dayfile.Change(self.now, {0: 5}))

        self.worker_transport.before = executor
        with (
            patch.object(service, "read_timing", return_value={}),
            patch.object(
                service.recovery, "prepare_learning", AsyncMock(return_value=(entities, current))
            ),
        ):
            learned = await service._learn_once(self.runtime, "a")
        self.assertEqual(learned["label_revision"], self.device.get("label_revision", 0))

    async def test_source_configuration_change_during_learning_rejects_installation(self):
        from unittest.mock import AsyncMock, patch

        from tuner_under_test.calibration import service

        entities, current = self.labelled()

        async def executor(request):
            self.device["presence_sources"] = {"external": ["binary_sensor.changed"]}

        self.worker_transport.before = executor
        with (
            patch.object(service, "read_timing", return_value={}),
            patch.object(
                service.recovery, "prepare_learning", AsyncMock(return_value=(entities, current))
            ),
            self.assertRaisesRegex(service.LearningEvidenceChanged, "sources"),
        ):
            await service._learn_once(self.runtime, "a")

    async def test_saved_source_configuration_change_rejects_apply_before_preparation(self):
        from unittest.mock import AsyncMock, patch

        from tuner_under_test.calibration import service

        entities, current = self.labelled()
        learned = self.runtime._fit_history("a", entities, current)
        learned["evidence_sources"] = service._learning_sources(self.runtime, "a")
        self.device["last_learning"] = learned
        self.device["presence_sources"] = {"external": ["binary_sensor.new_source"]}
        with patch.object(service.device_io, "prepare_device", AsyncMock()) as prepare:
            with self.assertRaisesRegex(ValueError, "sources"):
                await service._apply_validated(self.runtime, "a")
        prepare.assert_not_awaited()

    def test_earlier_snapshot_retains_open_pending_history(self):
        from tuner_under_test.calibration.adaptive import earlier_view
        from tuner_under_test.history import recording

        self.labelled()
        view = recording._history_view(self.runtime, "a")
        cutoff = self.now - 300
        earlier = earlier_view(view, cutoff)
        from tuner_under_test.history import spans

        cells = list(spans.events(earlier, "a"))
        self.assertAlmostEqual(sum(cell.end - cell.start for cell in cells), 1000, places=2)
        self.assertTrue(all(cell.end <= cutoff for cell in cells))

    def test_future_history_growth_cannot_change_earlier_snippets_or_mass(self):
        from tuner_under_test.calibration.adaptive import earlier_view
        from tuner_under_test.calibration.quality_evidence import collect
        from tuner_under_test.history import recording

        self.configuration()
        start = round(self.now - 20000, 3)
        row = bytes([40] + [255] * 17 + [1, 80])
        test_tuner.record_rows(self.runtime, "a", [(start + i, row) for i in range(1200)])
        self.device["history_labels"] = [
            {
                "start": start + block * 100,
                "end": start + block * 100 + 60,
                "state": "present" if block % 2 == 0 else "not_present",
            }
            for block in range(6)
        ]
        cutoff = start + 600

        def before():
            return collect(
                earlier_view(recording._history_view(self.runtime, "a"), cutoff), "a", ["g0_move"]
            )

        first = before()
        test_tuner.record_rows(self.runtime, "a", [(start + i, row) for i in range(1203, 13200)])
        second = before()
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(first.episodes, second.episodes)
        self.assertEqual(first.automatic_episodes, second.automatic_episodes)
        self.assertTrue(first.automatic_episodes)


def test_earlier_candidate_rejects_invalid_raw_confidence_before_correction():
    from types import SimpleNamespace

    from tuner_under_test.calibration import event_fitting
    from tuner_under_test.history import dayfile

    rows = _human_scene()
    view = SimpleNamespace(data={"devices": {"d": {}}, "room_snapshot": None})
    test_tuner.record_samples(view, "d", rows[:60])
    view._days.append("d", dayfile.Change(700, {0: 100}))
    view._days.append("d", dayfile.Guess(700, "present", 30, 0))  # 150% raw confidence.
    view._days.append("d", dayfile.End(706))
    test_tuner.record_samples(view, "d", rows[60:])
    result = event_fitting.fit(view, "d", ["g0_move"], {"g0_move": 25}, automatic_weight=0.5)
    assert result["adaptive_policy"]["selected"] == "reference"
    assert result["automatic_evidence"]["offered_samples"]["present"] == 0
    assert result["adaptive_policy"]["training_model"]["raw_episode_mass"]["present"] == 0


def test_selected_reference_reports_no_automatic_use_with_adequate_humans():

    rows = _human_scene()
    automatic = [(6000 + i * 6, {"g0_move": 30}, "present", 0.8) for i in range(60)]
    result = fit(
        rows,
        ["g0_move"],
        automatic=automatic,
        automatic_weight=0.5,
    )
    assert result["adaptive_policy"]["selected"] == "reference"
    assert result["automatic_evidence"]["used"] is False
    assert result["proposals"]["g0_move"]["auto_used"] is False


def test_insufficient_adaptive_fit_still_reports_policy_and_quality():
    result = fit([], ["g0_move"])
    assert result["status"] == "insufficient"
    assert len(result["adaptive_policy"]["model"]["fingerprint"]) == 64
    assert result["adaptive_policy"]["model"]["provenance"] == "recorded_basis_only"


def test_missing_human_presence_retains_prior_automatic_primary_at_zero_ceiling():
    human = [(1000 + i * 6, {"g0_move": 10}, "not_present") for i in range(60)]
    automatic = [(i * 6, {"g0_move": 40}, "present", 0.8) for i in range(60)]
    result = fit(human, ["g0_move"], automatic=automatic, automatic_weight=0)
    assert result["proposals"]["g0_move"]["threshold"] < 40
    assert result["automatic_evidence"]["used"]
    assert result["automatic_evidence"]["priority"] == "human_first"
    assert result["automatic_evidence"]["samples"]["present"] == 60

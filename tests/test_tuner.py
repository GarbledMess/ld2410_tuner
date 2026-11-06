"""The small-fixture harness (a runtime on one radar) and what is still best tested on it:
label semantics, apply safety, and learning on hand-written scenes."""

import asyncio
import shutil
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import ha_boundary

mod = ha_boundary.load_runtime()
constants = sys.modules["tuner_under_test.const"]
calibration_constants = sys.modules["tuner_under_test.calibration.constants"]
training = sys.modules["tuner_under_test.history.labels"]
label_lookup = sys.modules["tuner_under_test.history.label_lookup"]
ws = sys.modules["tuner_under_test.runtime.websocket"]
guesses = sys.modules["tuner_under_test.history.guesses"]
spans = sys.modules["tuner_under_test.history.spans"]


def fit(
    rows,
    keys,
    current=None,
    automatic=(),
    timing=None,
    automatic_weight=None,
    *,
    constraints=None,
    **_ignored,
):
    """The device learner (``event_fitting.fit``) on the samples of the sampled recorder
    (``record_samples``): labelled readings ``rows`` and automatic ones ``automatic``."""
    from tuner_under_test.calibration import event_fitting, settings

    if automatic_weight is None:
        automatic_weight = settings.DEFAULT_AUTOMATIC_WEIGHT
    view = types.SimpleNamespace(
        data={"devices": {"d": {}}, "room_snapshot": None}, _history_runtime={}
    )
    record_samples(view, "d", rows, automatic)
    return event_fitting.fit(
        view, "d", list(keys), current or {}, timing, automatic_weight, constraints=constraints
    )


def record_samples(view, device_id, rows, automatic=()):
    """Record the samples of the sampled recorder into ``view``'s day store: labelled readings
    ``rows`` ``(time, values, label)`` and automatic ones ``automatic``
    ``(time, values, label, confidence[, weight])``. Each reading was one sample, so it is
    recorded as ``SAMPLE_SECONDS`` of history (``sample_clock``): readings follow each other
    and a gap longer than ``GAP`` between them stays a gap. Consecutive labelled readings
    with one label are one labelled period (added to the radar's labels); an automatic
    reading is a guess of its confidence (external, so taken as given) over it."""
    from tuner_under_test.history import dayfile

    labelled = {ts: (values, label) for ts, values, label in rows}
    guessed = {ts: (values, label, confidence) for ts, values, label, confidence, *_ in automatic}
    clock = sample_clock(sorted({*labelled, *guessed}))
    records, labels = [], []
    for ts, (start, end, last) in clock.items():
        values = (labelled.get(ts) or guessed[ts])[0]
        vector = {gate: values.get(key) for gate, key in enumerate(constants.HISTORY_KEYS)}
        records.append(dayfile.Change(start, vector))
        if ts in labelled and labelled[ts][1] in ("present", "not_present", "unknown"):
            state = labelled[ts][1]
            if labels and labels[-1]["end"] == start and labels[-1]["state"] == state:
                labels[-1]["end"] = end  # One labelled stretch, as the samples were.
            else:
                labels.append({"start": start, "end": end, "state": state})
        elif ts in guessed and guessed[ts][1] in ("present", "not_present"):
            _values, label, confidence = guessed[ts]
            level = guesses.level(confidence * 100)
            records.append(dayfile.Guess(start, label, level, guesses.EXTERNAL))
            records.append(dayfile.Guess(end, None, 0, 0))
        if last:
            records.append(dayfile.End(end))
    device = view.data["devices"][device_id]
    device["history_labels"] = [*device.get("history_labels", []), *labels]
    days = _store(view)
    order = {dayfile.End: 0, dayfile.Guess: 1, dayfile.Change: 2}
    for record in sorted(records, key=lambda item: (item.time, order[type(item)])):
        days.append(device_id, record)
    if clock:
        end = max(end for _start, end, _last in clock.values())
        view._history_until = max(end, getattr(view, "_history_until", None) or end)
    return view


async def settled(runtime):
    """Let each pending label rebuild finish: a radar's histograms follow its label changes
    in the background. Their errors are raised here; a cancelled one (a clear) is skipped."""
    while pending := list(runtime._label_rebuilds.values()):
        for task in pending:
            try:
                await task
            except asyncio.CancelledError:
                if not task.cancelled():
                    raise


def sample_clock(times):
    """``{time: (start, end, last)}``: each of the sorted sample ``times`` as the
    ``SAMPLE_SECONDS`` of history it stood for, laid end to end; a gap longer than ``GAP``
    between samples stays (``last``: no sample follows within it)."""
    seconds = sys.modules["tuner_under_test.calibration.constants"].SAMPLE_SECONDS
    found, start = {}, None
    for index, ts in enumerate(times):
        if start is None or ts - times[index - 1] > guesses.GAP:
            start = ts if start is None else max(ts, start + seconds)
        else:
            start += seconds
        follows = index + 1 < len(times) and times[index + 1] - ts <= guesses.GAP
        found[ts] = (start, start + seconds, not follows)
    return found


def split_guesses(records):
    """Split test samples ``(time, row)`` whose rows end in a recorded guess ``(code, percent)``
    into ``(energy samples, guess runs)``: what the integration keeps since guesses became
    runs. Rows without a guess (18 energies) carry none."""
    samples = [(ts, row[:18]) for ts, row in records]
    guess = (guesses.from_code(*row[18:20]) if len(row) > 18 else None for _, row in records)
    return samples, guesses.from_samples(zip((ts for ts, _ in records), guess, strict=True))


def guessed(records):
    """``split_guesses`` with the runs as stored chunks."""
    samples, runs = split_guesses(records)
    return samples, guesses.encode(runs)


def pending(records):
    """The ``_history_runtime`` entry of a radar whose unstored ``records`` carry guesses."""
    samples, runs = split_guesses(records)
    return {"samples": samples, "runs": [list(run) for run in runs]}


def columns_of(samples):
    """A history view's ``_history_columns`` over ``samples`` ``(time, row)``: one batch."""
    energies = len(constants.HISTORY_KEYS)

    def read(*args, **kwargs):
        rows = [bytes(row[:energies]).ljust(energies, bytes([255])) for _, row in samples]
        times = [ts for ts, _ in samples]
        joined = b"".join(rows)
        return iter([(times, [joined[c::energies] for c in range(energies)])] if rows else [])

    return read


def set_records(view, device_id, records):
    """Make a ``stub_view`` read ``records`` instead (rows may end in a recorded guess)."""
    _store(view).remove_device(device_id)
    record_rows(view, device_id, records)


def _store(view):
    """``view``'s day store: a temporary one, removed with the view, when it has none."""
    from tuner_under_test.history import daystore

    days = getattr(view, "_days", None)
    if days is None:
        folder = tempfile.TemporaryDirectory()
        view._history_folder = folder  # Removed with the view.
        days = view._days = daystore.DayStore(Path(folder.name))
    return days


def record_rows(view, device_id, records, extra=()):
    """Record old-style test rows ``(time, row)`` into ``view``'s day store: ``row`` is 18
    energies (over 100: not read), optionally followed by a guess code and percent. They are
    recorded as conversion records stored rows (``convert``): each row holds until the next,
    and a gap longer than ``GAP`` ends coverage half a sample after the row before it. ``extra``
    records (guess spans) are recorded among them in time order."""
    from tuner_under_test.history import convert

    rows = {
        ts: (bytes(row[:18]), convert._from_code(*row[18:20]) if len(row) > 18 else None)
        for ts, row in records
    }
    found = sorted(
        [*convert._sampled(sorted(rows.items())), *extra],
        key=lambda record: (record.time, convert.ORDER.get(type(record), 3)),
    )
    days = _store(view)
    for record in found:
        days.append(device_id, record)
    if found:
        end = found[-1].time
        view._history_until = max(end, getattr(view, "_history_until", None) or end)
    return view


def recorded(view, device_id, events, end, *, open_until=None):
    """Record ``events`` ``[(time, 18-value vector)]`` for ``device_id`` into ``view``'s day
    store (a temporary one when it has none); coverage ends at ``end``, or with ``open_until``
    stays open (still recording) and is read up to that time. Call again for a later stretch
    after a gap. The view reads history as of the latest end recorded."""
    from tuner_under_test.history import dayfile

    days = _store(view)
    for timestamp, vector in events:
        days.append(device_id, dayfile.Change(timestamp, dict(enumerate(vector))))
    if open_until is None:
        days.append(device_id, dayfile.End(end))
    else:
        end = open_until
    view._history_until = max(end, getattr(view, "_history_until", None) or end)
    return view


def stub_view(device_id, device, records):
    """A minimal history view (what assessments and the room estimate read) of one radar whose
    ``records`` rows may end in a recorded guess (``record_rows``)."""
    view = types.SimpleNamespace(data={"devices": {device_id: dict(device)}}, _history_runtime={})
    return record_rows(view, device_id, records)


def episode_samples(present, negative, count=100, episodes=5):
    """``row_samples`` split into ``episodes`` alternating present and empty episodes, so the
    learner can hold later whole episodes out for chronological validation."""
    rows, size, t = [], count // episodes, 0
    for _ in range(episodes):
        for values, state in ((present, "present"), (negative, "not_present")):
            rows += [(t + i, dict(values), state) for i in range(size)]
            t += size
    return rows


def row_samples(present, negative, count=100):
    return [(i, dict(present), "present") for i in range(count)] + [
        (count + i, dict(negative), "not_present") for i in range(count)
    ]


class LearningTests(unittest.TestCase):
    def test_isolated_miss_within_targets_does_not_make_device_always_on(self):
        keys = ["g1_move", "g3_still"]
        rows = [
            (i * 6, {keys[0]: 7, keys[1]: 4 if i == 100 else 40}, "present") for i in range(2868)
        ]
        rows += [
            (30000 + i * 6, {keys[0]: 9 if i < 4316 else 4, keys[1]: 4}, "not_present")
            for i in range(5000)
        ]
        result = fit(rows, keys)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["training"]["false_negatives"], 1)
        self.assertEqual(result["outlier_filter"]["method"], "native_intervals_retained")
        self.assertFalse(result["outlier_filter"]["excluded"])
        self.assertEqual(result["training"]["false_positives"], 0)
        self.assertGreaterEqual(result["training"]["sensitivity"], 0.999)
        self.assertEqual(result["training"]["missed_presence_episodes"], 0)
        self.assertEqual(result["training"]["longest_missed_run_samples"], 1)
        self.assertEqual(result["recent_training"]["false_negatives"], 0)

    def test_recent_presence_reports_retained_dip_within_the_overall_time_budget(self):
        keys = ["g1_move", "g3_still"]
        rows = [
            (i * 6, {keys[0]: 7, keys[1]: 4 if i == 2867 else 40}, "present") for i in range(2868)
        ]
        rows += [(30000 + i * 6, {keys[0]: 9, keys[1]: 4}, "not_present") for i in range(5000)]
        result = fit(rows, keys)
        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(
            result["recent_training"]["duration"]["presence_recall"], 1 - 1 / (2868 * 0.2)
        )
        self.assertEqual(result["recent_training"]["false_negatives"], 1)
        self.assertEqual(result["recent_training"]["longest_missed_run_samples"], 1)
        self.assertEqual(result["training"]["false_positives"], 0)

    def test_brief_consecutive_misses_use_time_budget(self):
        keys = ["g1_move", "g3_still"]
        rows = [
            (
                i * 6 + (60 if i >= 100 else 0),
                {keys[0]: 7, keys[1]: 4 if i in (100, 101) else 40},
                "present",
            )
            for i in range(5000)
        ]
        rows += [(40000 + i * 6, {keys[0]: 9, keys[1]: 4}, "not_present") for i in range(5000)]
        result = fit(rows, keys)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["training"]["duration"]["missed_seconds"], 12)
        self.assertGreaterEqual(result["training"]["duration"]["presence_recall"], 0.999)
        self.assertEqual(result["training"]["false_positives"], 0)

    def test_overlapping_noisy_gates_do_not_trap_coordinate_search(self):
        keys = ["g0_move", "g1_move", "g2_still"]
        rows = [(i * 6, dict.fromkeys(keys, 30), "present") for i in range(1000)]
        rows += [
            (
                10000 + i * 6,
                {keys[0]: 20 if i < 6 else 10, keys[1]: 20 if i < 6 else 10, keys[2]: 10},
                "not_present",
            )
            for i in range(1000)
        ]
        result = fit(rows, keys)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["training"]["false_positives"], 0)
        self.assertEqual(result["training"]["false_negatives"], 0)
        self.assertTrue(all(p["threshold"] < 100 for p in result["proposals"].values()))

    def test_redundant_useful_gates_are_not_disabled(self):
        keys = list(constants.HISTORY_KEYS)
        result = fit(row_samples(dict.fromkeys(keys, 20), dict.fromkeys(keys, 10)), keys)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(all(10 < p["threshold"] < 20 for p in result["proposals"].values()))
        self.assertTrue(all(p["sensitivity"] == 1 for p in result["proposals"].values()))
        self.assertTrue(all(p["false_positives"] == 0 for p in result["proposals"].values()))

    def test_refit_learns_a_location_first_seen_in_recent_labels(self):
        rows = episode_samples({"g0_move": 30, "g1_still": 5}, {"g0_move": 5, "g1_still": 5})
        rows[179] = (179, {"g0_move": 5, "g1_still": 6}, "present")
        result = fit(rows, ["g0_move", "g1_still"])
        self.assertEqual(result["validation"]["false_negatives"], 1)
        self.assertEqual(result["training"]["false_negatives"], 0)
        self.assertEqual(result["status"], "ok")
        self.assertLess(result["proposals"]["g1_still"]["threshold"], 6)

    def test_partial_gate_observations_do_not_invent_full_channel_coverage(self):
        rows = [(i * 6, {"g0_move" if i % 2 else "g1_still": 20}, "present") for i in range(100)]
        rows += [
            (600 + i * 6, {"g0_move" if i % 2 else "g1_still": 5}, "not_present")
            for i in range(100)
        ]
        result = fit(rows, ["g0_move", "g1_still"])
        self.assertEqual(result["counts"], {"present": 0, "not_present": 0})
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(result["proposals"], {})

    def test_weak_still_signal_is_not_sacrificed(self):
        result = fit(episode_samples({"g0_still": 14}, {"g0_still": 10}), ["g0_still"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["proposals"]["g0_still"]["threshold"], 12)
        self.assertEqual(result["validation"]["sensitivity"], 1)

    def test_overlapping_classes_never_safe_at_zero_recall(self):
        result = fit(row_samples({"g0_move": 10}, {"g0_move": 10}), ["g0_move"])
        self.assertEqual(result["status"], "unsafe")
        # Detecting indistinguishable classes would violate the mirror guard.
        self.assertEqual(result["training"]["sensitivity"], 0)
        self.assertEqual(result["training"]["false_positive_rate"], 0)
        self.assertEqual(result["training"]["missed_presence_episodes"], 1)

    def test_equality_does_not_trigger(self):
        metrics = sys.modules["tuner_under_test.calibration.metrics"].metrics
        result = metrics(row_samples({"g0_move": 100}, {"g0_move": 100}), {"g0_move": 100})
        self.assertEqual(result["sensitivity"], 0)
        self.assertEqual(result["false_positives"], 0)

    def test_different_locations_use_complementary_gates(self):
        rows = episode_samples({"g0_move": 30, "g1_still": 5}, {"g0_move": 5, "g1_still": 5})
        for i, (timestamp, _values, state) in enumerate(rows):
            if state == "present" and i % 2 == 0:
                rows[i] = (timestamp, {"g0_move": 5, "g1_still": 20}, state)
        result = fit(rows, ["g0_move", "g1_still"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["validation"]["sensitivity"], 1)
        self.assertTrue(all(p["sensitivity"] == 0.5 for p in result["proposals"].values()))

    def test_held_out_background_drift_reports_a_time_penalty(self):
        rows = episode_samples({"g0_move": 30}, {"g0_move": 5})
        rows[-20:] = [(i, {"g0_move": 40}, "not_present") for i in range(180, 200)]
        result = fit(rows, ["g0_move"], current={"g0_move": 20})
        self.assertEqual(result["training"]["false_positive_rate"], 0.2)
        self.assertEqual(result["validation"]["false_positive_rate"], 1)
        self.assertEqual(result["status"], "tradeoff")

    def test_missing_gates_not_imputed_as_zero(self):
        result = fit(
            row_samples({"g0_move": 30}, {"g0_move": 5}),
            ["g0_move", "g1_move"],
            current={"g0_move": 20, "g1_move": 42},
        )
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(result["counts"], {"present": 0, "not_present": 0})
        self.assertEqual(result["proposals"], {})

    def test_false_positive_penalty_is_for_whole_device(self):
        # Each gate's noise spikes occur at different times; pooling per-gate
        # allowances would exceed the whole-device budget.
        keys = ["g0_move", "g1_move"]
        rows = [
            (i, {keys[0]: 30 if i % 2 else 5, keys[1]: 5 if i % 2 else 30}, "present")
            for i in range(1000)
        ]
        negatives = [(1000 + i, dict.fromkeys(keys, 5), "not_present") for i in range(1000)]
        for i in range(4):
            negatives[i][1][keys[0]] = 40
        for i in range(4, 8):
            negatives[i][1][keys[1]] = 40
        # Begin with both complementary locations detected; the measurement
        # checks the whole-device union independently of search initialization.
        result = fit(rows + negatives, keys, current=dict.fromkeys(keys, 20))
        self.assertEqual(result["training"]["false_positive_rate"], 0.008)
        self.assertEqual(result["status"], "tradeoff")
        self.assertEqual(result["training"]["false_positives"], 8)
        for proposal in result["proposals"].values():
            self.assertEqual(proposal["false_positives"], 4)
            self.assertEqual(proposal["not_present_samples"], 1000)
            self.assertEqual(proposal["message"], "")
        self.assertAlmostEqual(result["training"]["duration"]["false_positive_score"], -0.8)

    def test_guesses_cover_additional_location_at_lower_weight(self):
        keys = ["g0_move", "g1_still"]
        rows = row_samples({keys[0]: 30, keys[1]: 5}, dict.fromkeys(keys, 5))
        # The new location clears the automatic positive-support floor and has
        # enough independent cost to exceed the hybrid movement evidence band.
        guesses = [(-100 + i, {keys[0]: 5, keys[1]: 12}, "present", 0.6) for i in range(100)]
        current = {keys[0]: 20, keys[1]: 13}
        manual_only = fit(rows, keys, current=current)
        # Episode support uses confidence0.6 times the conservative reliability prior0.75.
        assisted = fit(rows, keys, current=current, automatic=guesses, automatic_weight=0.3)
        self.assertEqual(manual_only["proposals"][keys[1]]["threshold"], 13)
        self.assertLess(assisted["proposals"][keys[1]]["threshold"], 12)
        self.assertTrue(assisted["automatic_evidence"]["used"])
        self.assertAlmostEqual(
            assisted["automatic_evidence"]["share"]["present"], 0.075 * 0.45 / 1.45
        )
        self.assertEqual(assisted["validation"], manual_only["validation"])

    def test_confidence_changes_the_share_and_volume_is_capped(self):
        rows = row_samples({"g0_move": 30}, {"g0_move": 5})

        def weighted(count, confidence):
            guesses = [(-count + i, {"g0_move": 25}, "present", confidence) for i in range(count)]
            return fit(rows, ["g0_move"], automatic=guesses, automatic_weight=0.2)[
                "automatic_evidence"
            ]["share"]["present"]

        self.assertGreater(weighted(50, 0.9), weighted(50, 0.6))
        # Plentiful guesses: the mass ratio caps at 1, so the share is 0.2 x 0.25.
        self.assertAlmostEqual(weighted(1000, 0.99), 0.05 * 0.75 / (1 + 0.75))

    def test_automatic_only_recommendation_is_usable_but_identified_as_estimated(self):
        guesses = [
            (i, {"g0_move": 30 if i < 100 else 5}, "present" if i < 100 else "not_present", 0.65)
            for i in range(200)
        ]
        result = fit([], ["g0_move"], automatic=guesses)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["evidence_basis"], "automatic")
        self.assertIsNone(result["validation"])
        self.assertLess(result["proposals"]["g0_move"]["threshold"], 30)

    def test_newer_guesses_are_used_by_final_fit_without_changing_backtest(self):
        rows = episode_samples({"g0_move": 30}, {"g0_move": 5})
        result = fit(rows, ["g0_move"], automatic=[(300, {"g0_move": 50}, "not_present", 0.99)])
        self.assertEqual(result["automatic_evidence"]["deferred_samples"], 0)
        self.assertEqual(result["automatic_evidence"]["offered_samples"]["not_present"], 1)
        self.assertEqual(result["adaptive_policy"]["selected"], "reference")
        self.assertEqual(result["validation"], fit(rows, ["g0_move"])["validation"])

    def test_one_missed_validation_sample_is_not_almost_perfect(self):
        rows = episode_samples({"g0_move": 30}, {"g0_move": 5})
        rows[179] = (179, {"g0_move": 5}, "present")
        result = fit(rows, ["g0_move"])
        self.assertEqual(result["validation"]["sensitivity"], 0.95)
        self.assertEqual(result["status"], "unsafe")
        self.assertEqual(result["targets"]["sensitivity"], 0.999)

    def test_episode_and_burst_metrics_reveal_temporal_failures(self):
        metrics = sys.modules["tuner_under_test.calibration.metrics"].metrics
        rows = [
            (0, {"g0_move": 30}, "present"),
            (6, {"g0_move": 30}, "present"),
            (30, {"g0_move": 5}, "present"),
            (36, {"g0_move": 5}, "present"),
            (60, {"g0_move": 30}, "not_present"),
            (66, {"g0_move": 30}, "not_present"),
            (72, {"g0_move": 5}, "not_present"),
            (78, {"g0_move": 30}, "not_present"),
        ]
        result = metrics(rows, {"g0_move": 10})
        self.assertEqual(result["presence_episodes"], 2)
        self.assertEqual(result["missed_presence_episodes"], 1)
        self.assertEqual(result["longest_missed_run_samples"], 2)
        self.assertEqual(result["false_trigger_bursts"], 2)

    def test_near_perfect_average_cannot_hide_entire_missed_episode(self):
        rows = episode_samples({"g0_move": 30}, {"g0_move": 5}, count=5000)
        rows = [(t * 6, values, state) for t, values, state in rows]
        rows[8999] = (8999 * 6 + 120, {"g0_move": 5}, "present")
        rows[9000:] = [(t + 120, values, state) for t, values, state in rows[9000:]]
        result = fit(rows, ["g0_move"])
        self.assertEqual(result["validation"]["present_samples"], 1)
        self.assertEqual(result["validation"]["sensitivity"], 0)
        self.assertEqual(result["validation"]["missed_presence_episodes"], 1)
        self.assertEqual(result["status"], "unsafe")

    def test_frequent_bursts_remain_visible_without_failing_the_job(self):
        rows = episode_samples({"g0_move": 30}, {"g0_move": 5}, count=5000)
        for i in (9100, 9400, 9800):
            rows[i][1]["g0_move"] = 40
        result = fit(rows, ["g0_move"])
        self.assertLessEqual(result["validation"]["false_positive_rate"], 0.005)
        self.assertGreater(result["validation"]["false_trigger_bursts_per_hour"], 1)
        self.assertEqual(result["status"], "tradeoff")


class InferenceTests(unittest.TestCase):
    def histogram(self, value):
        hist = [0] * 101
        hist[value] = 100
        return hist

    def test_weak_stationary_signal_accumulates_over_time(self):
        infer = sys.modules["tuner_under_test.presence.inference"].estimate_presence
        manual = {"g0_still": {"present": self.histogram(14), "not_present": self.histogram(10)}}
        temporal = {}
        results = [
            infer({"g0_still": 13}, manual, {}, temporal, 100 + i * 2, ["g0_still"])
            for i in range(8)
        ]
        self.assertEqual(results[-1]["label"], "present")
        self.assertGreater(results[-1]["confidence"], 0.9)
        self.assertGreater(results[-1]["presence_probability"], results[0]["presence_probability"])

    def test_move_and_still_from_same_gate_not_double_counted(self):
        infer = sys.modules["tuner_under_test.presence.inference"].estimate_presence
        manual = {
            key: {"present": self.histogram(20), "not_present": self.histogram(5)}
            for key in ("g0_move", "g0_still")
        }
        single = infer({"g0_move": 20}, manual, {}, {}, 100, ["g0_move"])
        paired = infer({"g0_move": 20, "g0_still": 20}, manual, {}, {}, 100, list(manual))
        self.assertEqual(single["score"], paired["score"])

    def test_missing_gate_is_unknown_not_absent(self):
        infer = sys.modules["tuner_under_test.presence.inference"].estimate_presence
        result = infer(
            {"g0_move": 5}, {}, {"g0_move": self.histogram(5)}, {}, 100, ["g0_move", "g1_move"]
        )
        self.assertEqual(result["label"], "unknown")

    def test_uninformative_channels_do_not_hide_learned_absence(self):
        infer = sys.modules["tuner_under_test.presence.inference"].estimate_presence
        manual = {
            "g0_still": {"present": self.histogram(20), "not_present": self.histogram(5)},
            "g0_move": {"present": self.histogram(5), "not_present": self.histogram(5)},
            "g1_move": {"present": self.histogram(5), "not_present": self.histogram(5)},
        }
        temporal = {"probability": 0.99, "timestamp": 100}
        for i in range(8):
            result = infer(
                dict.fromkeys(manual, 5), manual, {}, temporal, 100 + i * 2, list(manual)
            )
        self.assertEqual(result["label"], "not_present")
        self.assertGreater(result["confidence"], 0.9)

    def test_bootstrap_confidence_is_capped(self):
        infer = sys.modules["tuner_under_test.presence.inference"].estimate_presence
        temporal = {}
        result = None
        for i in range(10):
            result = infer(
                {"g0_move": 30},
                {},
                {"g0_move": self.histogram(5)},
                temporal,
                100 + i * 2,
                ["g0_move"],
            )
        self.assertEqual(result["label"], "present")
        self.assertEqual(result["confidence"], 0.65)

    def test_common_empty_energy_cannot_become_positive_from_label_frequency(self):
        infer = sys.modules["tuner_under_test.presence.inference"].estimate_presence
        empty = self.histogram(5)
        empty[5], empty[3] = 20, 80
        manual = {"g0_move": {"present": self.histogram(5), "not_present": empty}}
        temporal = {}
        results = [
            infer({"g0_move": 5}, manual, {}, temporal, 100 + i * 2, ["g0_move"]) for i in range(60)
        ]
        self.assertNotIn("present", [result["label"] for result in results])

    def test_novel_strong_presence_does_not_need_identical_human_example(self):
        infer = sys.modules["tuner_under_test.presence.inference"].estimate_presence
        manual = {"g0_move": {"present": self.histogram(20), "not_present": self.histogram(5)}}
        result = infer({"g0_move": 60}, manual, {}, {}, 100, ["g0_move"])
        self.assertEqual(result["label"], "present")

    def test_partial_presence_needs_direct_human_distribution_support(self):
        infer = sys.modules["tuner_under_test.presence.inference"].estimate_presence
        expected = ["g0_move", "g1_move"]
        background = {"g0_move": self.histogram(5)}
        guessed = infer({"g0_move": 30}, {}, background, {}, 100, expected)
        self.assertEqual(guessed["label"], "unknown")
        manual = {"g0_move": {"present": self.histogram(30), "not_present": self.histogram(5)}}
        guided = infer({"g0_move": 30}, manual, background, {}, 100, expected)
        self.assertEqual(guided["label"], "present")
        self.assertLessEqual(guided["confidence"], 0.60)

    def test_flat_startup_never_certifies_empty(self):
        infer = sys.modules["tuner_under_test.presence.inference"].estimate_presence
        temporal = {}
        for i in range(100):
            result = infer(
                {"g0_still": 30},
                {},
                {"g0_still": self.histogram(30)},
                temporal,
                100 + i * 2,
                ["g0_still"],
            )
            self.assertEqual(result["label"], "unknown")
            self.assertEqual(result["confidence"], 0)

    def test_weak_positive_is_reduced_by_contradictory_evidence(self):
        combine = sys.modules["tuner_under_test.presence.inference"]._combine_evidence
        self.assertLess(combine([0.1, -1, -1]), 0)
        self.assertGreater(combine([3, -1, -1]), 0)

    def test_filter_integrates_elapsed_time_without_extra_callback_votes(self):
        advance = sys.modules["tuner_under_test.presence.inference"]._advance
        regular = 0.5
        for _ in range(3):
            regular = advance(regular, 0.6, 2)
        delayed = advance(0.5, 0.6, 6)
        self.assertAlmostEqual(regular, delayed, delta=0.02)
        self.assertEqual(advance(0.5, 0.6, 0), 0.5)

    def test_confirmation_uses_time_and_resets_after_gaps(self):
        confirm = sys.modules["tuner_under_test.presence.inference"].confirm_estimate
        state = {}
        result = {"label": "present", "score": 2, "model": "test"}
        for i in range(20):
            self.assertEqual(confirm(result, state, 100 + i * 0.1), "unknown")
        self.assertEqual(confirm(result, state, 104), "present")
        self.assertEqual(confirm(result, state, 120), "unknown")
        self.assertEqual(confirm(result, state, 124), "present")
        self.assertEqual(confirm(result, state, 123), "unknown")

    def test_model_upgrade_discards_old_filter_certainty(self):
        infer = sys.modules["tuner_under_test.presence.inference"].estimate_presence
        temporal = {"probability": 0.99, "timestamp": 100, "model": "old"}
        result = infer({"g0_move": 5}, {}, {}, temporal, 102, ["g0_move"])
        self.assertEqual(result["label"], "unknown")
        self.assertEqual(result["presence_probability"], 0.5)

    def test_inference_never_mutates_human_reference_histograms(self):
        import copy

        infer = sys.modules["tuner_under_test.presence.inference"].estimate_presence
        manual = {"g0_move": {"present": self.histogram(20), "not_present": self.histogram(5)}}
        original = copy.deepcopy(manual)
        infer({"g0_move": 20}, manual, {}, {}, 100, ["g0_move"])
        self.assertEqual(manual, original)


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from tuner_under_test.worker.supervisor import Supervisor
        from worker_transport import Transport

        self.worker_transport = Transport()

        async def process(service, args):
            return await self.worker_transport.process(args)

        self.worker_patch = patch.object(Supervisor, "_process", process)
        self.worker_patch.start()
        self.addCleanup(self.worker_patch.stop)
        self.states = {}
        self.report_write = types.MethodType(RuntimeTests.report_write, self)
        io = sys.modules["tuner_under_test.calibration.device_io"]
        self.settle = patch.object(io, "SETTLE_SECONDS", 0)
        self.settle.start()
        self.addCleanup(self.settle.stop)
        self.registry = types.SimpleNamespace(entities={})
        self.registry.async_get = self.registry.entities.get
        self.registry.async_get_entity_id = lambda domain, platform, unique_id: next(
            (
                entity_id
                for entity_id, entry in self.registry.entities.items()
                if entity_id.startswith(f"{domain}.")
                and entry.platform == platform
                and entry.unique_id == unique_id
            ),
            None,
        )
        mod.er.async_get = lambda hass: self.registry
        config = tempfile.mkdtemp(prefix="ld2410-config-")
        self.addCleanup(shutil.rmtree, config, ignore_errors=True)
        self.hass = types.SimpleNamespace(
            config=types.SimpleNamespace(path=lambda *parts: str(Path(config, *parts))),
            async_add_executor_job=lambda fn, *args: asyncio.to_thread(fn, *args),
            states=types.SimpleNamespace(get=self.states.get),
            services=types.SimpleNamespace(async_call=AsyncMock(side_effect=self.report_write)),
            async_create_task=asyncio.create_task,
            async_create_background_task=lambda target, name: asyncio.create_task(target),
        )
        self.device = {
            "entities": {"sensor.radar_g0_move_energy": {"gate": 0, "kind": "move"}},
            "training_state": "unknown",
        }
        self.runtime = mod.TunerRuntime(
            self.hass,
            types.SimpleNamespace(async_save=AsyncMock()),
            {"devices": {"a": self.device}},
        )
        self.runtime._schedule_save = lambda: None
        self.runtime._schedule_flush = lambda: None  # Day-log appends: the same boundary.
        self.now = time.time()
        from tuner_under_test.history.daystore import DayStore

        self.runtime._days = DayStore(
            Path(self.hass.config.path(".storage", "ld2410_tuner", "history"))
        )

    async def report_write(self, domain, service, data, **kwargs):
        if domain == "number" and service == "set_value":
            self.states[data["entity_id"]] = types.SimpleNamespace(state=str(data["value"]))

    async def asyncTearDown(self):
        from tuner_under_test.history import commit
        from tuner_under_test.worker import bridge

        # A label recount left in a worker thread would hold its input open past cleanup; a
        # test that holds the worker forever (unload tests) is not waited for.
        if rebuilds := list(self.runtime._label_rebuilds.values()):
            await asyncio.wait(rebuilds, timeout=5)
        await bridge.stop(self.runtime)
        await commit.drain(self.runtime)
        tasks = list(self.runtime._timeout_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def sample(self, timestamp, value=10):
        self.states["sensor.radar_g0_move_energy"] = types.SimpleNamespace(state=str(value))
        with patch.object(time, "time", return_value=timestamp):
            self.runtime.sample_devices()

    def test_fast_label_reader_matches_priority_and_boundaries(self):
        self.device["history_labels"] = [
            {"start": 0, "end": 30, "state": "present"},
            {"start": 10, "end": 20, "state": "unknown"},
            {"start": 15, "end": 25, "state": "not_present"},
        ]
        self.device.update(
            training_state="present", training_label_start=28, training_expires_at=35
        )
        read = label_lookup._history_label_reader(self.device)
        for timestamp in (-1, 0, 9, 10, 15, 19, 20, 24, 25, 28, 30, 34, 35, 100):
            self.assertEqual(
                read(timestamp), self.runtime._manual_history_state(self.device, timestamp)
            )

    async def test_changed_labels_invalidate_old_recommendation(self):
        self.configuration()
        self.device["last_learning"]["label_revision"] = 0
        self.device["label_revision"] = 1
        with self.assertRaisesRegex(ValueError, "labels changed"):
            await self.runtime.apply("a")

    def test_constant_signal_is_counted_without_state_events(self):
        self.device.update(training_state="present", training_label_start=self.now - 1)
        for i in range(3):
            self.sample(self.now + i * 6)
        # Polls at 0, 6 and 12 s observe 12 s of an unchanged reading: two 6 s units.
        self.assertAlmostEqual(sum(self.device["histograms"]["g0_move"]["present"]), 2, places=2)

    def test_unavailable_reading_is_not_reused(self):
        self.sample(self.now)
        self.sample(self.now + 6, "unavailable")
        self.assertEqual(self.runtime._live["a"], {})
        with patch.object(time, "time", return_value=self.now + 9):
            cells = [(c.start, c.end, c.values[0]) for c in spans.events(self.runtime, "a")]
        start = cells[0][0]
        self.assertEqual([value for _a, _b, value in cells], [10, None])
        self.assertAlmostEqual(cells[0][1] - start, 6, places=2)

    async def test_relabel_is_idempotent_and_does_not_duplicate_initial_training(self):
        self.device.update(training_state="present", training_label_start=self.now - 20)
        self.sample(self.now - 12)
        self.sample(self.now - 6)
        with patch.object(time, "time", return_value=self.now - 1):
            for _ in range(2):
                await self.runtime.label_history_range(
                    "a", self.now - 15, self.now - 1, "not_present"
                )
            await settled(self.runtime)
        hist = self.device["histograms"]["g0_move"]
        self.assertEqual(sum(hist["present"]), 0)
        self.assertAlmostEqual(sum(hist["not_present"]), 11 / 6, places=2)  # [-12, -1) s.

    async def test_unknown_clears_only_requested_half_open_range(self):
        self.device.update(training_state="present", training_label_start=self.now - 20)
        self.sample(self.now - 12)
        self.sample(self.now - 6)
        with patch.object(time, "time", return_value=self.now):
            await self.runtime.label_history_range("a", self.now - 12, self.now - 6, "unknown")
        hist = self.device["histograms"]["g0_move"]
        self.assertAlmostEqual(sum(hist["present"]), 1, places=2)  # [-6, 0) s stays present.

    def test_clear_invalidates_learned_and_stops_training(self):
        self.device.update(
            training_state="present",
            training_label_start=self.now - 20,
            last_learning={"status": "ok"},
        )
        self.sample(self.now - 6)
        self.runtime.clear_samples("a")
        self.assertNotIn("last_learning", self.device)
        self.assertEqual(self.device["training_state"], "unknown")
        self.assertEqual(list(spans.events(self.runtime, "a")), [])

    def test_far_apart_readings_keep_their_exact_times_across_days(self):
        self.runtime._record_history_sample("a", {"g0_move": 10}, self.now - 70000)
        self.runtime._record_history_sample("a", {"g0_move": 20}, self.now)
        with patch.object(time, "time", return_value=self.now + 1):  # Read after the reading.
            changes = [(c.start, c.values[0]) for c in spans.events(self.runtime, "a")]
        self.assertAlmostEqual(changes[0][0], self.now - 70000, places=2)
        self.assertEqual(changes[0][1], 10)
        self.assertAlmostEqual(changes[-1][0], self.now, places=2)
        self.assertEqual(changes[-1][1], 20)

    async def test_cancelled_timeout_cannot_remove_replacement(self):
        self.runtime.set_training_state("a", "present", 100)
        await asyncio.sleep(0)
        old_task = self.runtime._timeout_tasks["a"]
        self.runtime.set_training_state("a", "not_present", 200)
        new_task = self.runtime._timeout_tasks["a"]
        await asyncio.sleep(0)
        self.assertIs(self.runtime._timeout_tasks["a"], new_task)
        self.assertTrue(old_task.cancelled())

    def test_expired_training_does_not_label_restart_gap(self):
        self.device.update(
            training_state="present",
            training_label_start=self.now - 30,
            training_expires_at=self.now - 20,
        )
        self.runtime._close_training_interval(self.device, self.now)
        self.assertEqual(self.device["history_labels"][0]["end"], self.now - 20)

    def labelled(self):
        self.configuration()
        # Every configured gate is recorded: a gate without readings leaves no coverage.
        quiet = [0] * 5 + [255] * 12
        row = {40: bytes([40, *quiet, 0, 0]), 5: bytes([5, *quiet, 0, 0])}
        # 100 present and 100 empty samples, each six seconds of recording.
        start = self.now - 1300
        record_rows(
            self.runtime, "a", [(start + i * 6, row[40 if i < 100 else 5]) for i in range(200)]
        )
        self.device["history_labels"] = [
            {"start": start, "end": start + 600, "state": "present"},
            {"start": start + 600, "end": start + 1200, "state": "not_present"},
        ]
        return self.runtime._threshold_configuration("a")

    def configuration(self):
        for i in range(3):
            entity_id = f"number.radar_g{i}_move_threshold"
            self.registry.entities[entity_id] = types.SimpleNamespace(
                device_id="a", domain="number", entity_id=entity_id
            )
            self.states[entity_id] = types.SimpleNamespace(state="10")
        for i in range(3):
            still_id = f"number.radar_g{i}_still_threshold"
            self.registry.entities[still_id] = types.SimpleNamespace(
                device_id="a", domain="number", entity_id=still_id
            )
            self.states[still_id] = types.SimpleNamespace(state="10")
        for kind, limit in (("move", 2), ("still", 2)):
            entity_id = f"number.radar_max_{kind}_distance_gate"
            self.registry.entities[entity_id] = types.SimpleNamespace(
                device_id="a", domain="number", entity_id=entity_id
            )
            self.states[entity_id] = types.SimpleNamespace(state=str(limit))
        entities, current = self.runtime._threshold_configuration("a")
        self.device["last_learning"] = {
            "method": sys.modules["tuner_under_test.calibration.constants"].METHOD,
            **sys.modules["tuner_under_test.calibration.settings"].signature(self.runtime.data),
            "status": "ok",
            "entities": entities,
            "configuration": current,
            "proposals": {key: {"threshold": 20} for key in entities},
        }

    async def test_unsafe_recommendation_can_be_applied_explicitly(self):
        self.configuration()
        learned = self.device["last_learning"]
        learned["status"] = "unsafe"
        learned["feasibility"] = {"status": "conflict"}
        learned["warnings"] = ["Combined thresholds miss the targets"]
        for proposal in learned["proposals"].values():
            proposal["status"] = "unsafe"
        result = await self.runtime.apply("a")
        self.assertEqual(result["applied"], dict.fromkeys(learned["entities"], 20))
        self.assertEqual(result["skipped"], {})
        self.assertEqual(self.hass.services.async_call.await_count, 6)
        self.assertEqual(learned["status"], "unsafe")

    async def test_apply_rejects_missing_or_invalid_proposals_before_writing(self):
        for proposals in ({}, {"g0_move": {"threshold": 20}}):
            with self.subTest(proposals=proposals):
                self.configuration()
                self.device["last_learning"]["proposals"] = proposals
                with self.assertRaisesRegex(ValueError, "No complete learned thresholds"):
                    await self.runtime.apply("a")
        for value in (None, True, "20", float("nan"), float("inf"), -1, 101):
            with self.subTest(value=value):
                self.configuration()
                self.device["last_learning"]["proposals"]["g0_move"]["threshold"] = value
                with self.assertRaisesRegex(ValueError, "Invalid learned threshold"):
                    await self.runtime.apply("a")
        self.hass.services.async_call.assert_not_awaited()

    async def test_apply_stops_after_partial_failure(self):
        self.configuration()

        async def fail_second(domain, service, data, **kwargs):
            if self.hass.services.async_call.await_count > 1:
                raise RuntimeError("offline")
            await self.report_write(domain, service, data, **kwargs)

        self.hass.services.async_call.side_effect = fail_second
        result = await self.runtime.apply("a")
        self.assertEqual(len(result["applied"]), 1)
        self.assertEqual(len(result["skipped"]), 5)
        self.assertEqual(self.hass.services.async_call.await_count, 2)

    async def test_apply_requires_preview_and_unchanged_configuration(self):
        with self.assertRaises(ValueError):
            await self.runtime.apply("a")
        self.configuration()
        self.states["number.radar_g0_move_threshold"].state = "30"
        with self.assertRaises(ValueError):
            await self.runtime.apply("a")
        self.hass.services.async_call.assert_not_awaited()

    async def test_nonfinite_history_range_rejected(self):
        with self.assertRaises(ValueError):
            await self.runtime.label_history_range("a", float("nan"), self.now, "present")
        with self.assertRaises(ValueError):
            self.runtime.history_series_multi("a", ["g0_move"], float("inf"))

    def test_unmanaged_gate_blocks_device_wide_learning(self):
        self.configuration()
        self.registry.entities.pop("number.radar_g1_move_threshold")
        with self.assertRaisesRegex(ValueError, "Enable all active"):
            self.runtime._threshold_configuration("a")

    async def test_concurrent_apply_is_rejected(self):
        self.configuration()
        entered, release = asyncio.Event(), asyncio.Event()

        async def write(*args, **kwargs):
            entered.set()
            await release.wait()
            await self.report_write(*args, **kwargs)

        self.hass.services.async_call.side_effect = write
        first = asyncio.create_task(self.runtime.apply("a"))
        await entered.wait()
        with self.assertRaisesRegex(ValueError, "already in progress"):
            await self.runtime.apply("a")
        release.set()
        await first
        self.assertEqual(self.runtime._applying, set())

    def test_short_spike_cannot_confirm_entry_from_filter_memory(self):
        for i in range(45):
            self.sample(self.now + i * 2, 5)
        states = []
        for i in range(45, 65):
            self.sample(self.now + i * 2, 30 if i < 47 else 5)
            states.append(self.device["auto"]["last_classification"]["state"])
        self.assertNotIn("present", states)

    async def test_manual_corrections_and_explicit_unknown_override_guesses(self):
        self.now = round(self.now, 3)
        self.configuration()
        keys = list(self.device["last_learning"]["entities"])
        values = bytes(30 if key in keys else 255 for key in constants.HISTORY_KEYS)
        record_rows(
            self.runtime,
            "a",
            [(self.now - 12, values + bytes((1, 90))), (self.now - 6, values + bytes((1, 90)))],
        )
        await self.runtime.label_history_range("a", self.now - 15, self.now - 9, "not_present")
        await self.runtime.label_history_range("a", self.now - 9, self.now - 1, "unknown")
        entities, current = self.runtime._threshold_configuration("a")
        result = self.runtime._fit_history("a", entities, current)
        # The empty label covers 3 s of the first reading (held until the next): half a sample.
        self.assertAlmostEqual(result["counts"]["not_present"], 0.5, places=4)
        self.assertEqual(result["automatic_evidence"]["samples"]["present"], 0)

    async def test_previous_95_percent_model_cannot_be_applied(self):
        self.configuration()
        for previous in ("joint_labelled_v1", "human_priority_v3"):
            self.device["last_learning"]["method"] = previous
            with self.assertRaises(ValueError):
                await self.runtime.apply("a")
        self.hass.services.async_call.assert_not_awaited()

    async def test_a_reading_recorded_during_cleanup_is_kept(self):
        self.sample(self.now - 10)

        async def executor(fn, *args):
            result = await asyncio.to_thread(fn, *args)
            self.sample(self.now, 30)
            return result

        self.hass.async_add_executor_job = AsyncMock(side_effect=executor)
        await self.runtime.async_clean_history()
        with patch.object(time, "time", return_value=self.now + 1):  # Read after the reading.
            values = [c.values[0] for c in spans.events(self.runtime, "a")]
        self.assertEqual(values[0], 10)
        self.assertEqual(values[-1], 30)


if __name__ == "__main__":
    unittest.main(verbosity=2)

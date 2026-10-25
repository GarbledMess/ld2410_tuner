"""Raw learner guesses stay separate from trusted reference and display attribution."""

import unittest
from unittest.mock import patch

import test_rooms
from tuner_under_test.history import dayfile, guesses
from tuner_under_test.rooms import attribution


class RawLearningAttributionTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_rooms.RoomAttributionTests.setUp
    asyncTearDown = test_rooms.RoomAttributionTests.asyncTearDown
    office = test_rooms.RoomAttributionTests.office

    def test_learning_window_clips_reused_rows_at_the_exclusive_cutoff(self):
        self.office()
        start = self.now - 120
        for offset in range(0, 121, 6):  # A change at every reading: one span each.
            value = 60 + offset // 6 % 2
            self.runtime._record_history_sample("a", {"g0_move": value}, start + offset)

        from tuner_under_test.history import spans
        from tuner_under_test.rooms import attribution_days

        def read(until=None):  # The spans attribution reads, a day at a time.
            view = attribution.prepared_view(self.runtime, "a")
            if until is not None:
                view._attribution_until = until
            found = []
            real = spans.events

            def events(*args, **kwargs):
                items = list(real(*args, **kwargs))
                found.extend(items)
                return items

            with patch.object(attribution_days.spans, "events", side_effect=events):
                attribution.radar_attribution(view, "a", view.data["room_snapshot"])
            return [item.start for item in found]

        full, cutoff = read(), start + 30
        self.assertEqual(read(cutoff), [t for t in full if t < cutoff])
        self.assertLess(len(read(cutoff)), len(full))

    def test_raw_reader_reaches_derived_room_truth_without_changing_default(self):
        self.office()
        start = self.now - 120
        for key in ("a", "b"):
            for offset in range(0, 360, 6):
                self.runtime._record_history_sample(key, {"g0_move": 2}, start - 500 + offset)
            self.runtime.data["devices"][key]["history_labels"] = [
                {"start": start - 500, "end": start - 140, "state": "not_present"}
            ]
            device = self.runtime.data["devices"][key]
            device["label_revision"] = device.get("label_revision", 0) + 1
            for offset in range(0, 121, 6):
                self.runtime._record_history_sample(key, {"g0_move": 60}, start + offset)
            # The radar's own present guess at 80 % over the loud minutes, confirmed.
            self.runtime._days.append(
                key, dayfile.GuessSpan(self.now, start, self.now, "present", 16, guesses.REFERENCE)
            )
        view = attribution.prepared_view(self.runtime, "a")
        self.assertIsNone(attribution.auto_targets(view, "a"))
        raw = guesses.of(view, "a")
        target = attribution.auto_targets(view, "a", reader=raw)
        self.assertIsNotNone(target)
        # Two independent .8 member guesses combine through the existing union rule.
        self.assertEqual(target(start + 60), ("present", "estimate", 0.96))
        self.assertIsNone(attribution.auto_targets(view, "a"))

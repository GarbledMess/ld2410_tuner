"""Learning may finish after appends, but cannot install over rewritten or removed evidence.

A job's view reads the day store as of when it was taken; recording only appends, so its
inputs stay owned unless a finished day was rewritten (thinning) or deleted (retention, Trim
now), or the radar was cleared or removed.
"""

import unittest

import test_automatic_weight
import test_tuner  # noqa: F401
from tuner_under_test.calibration.ownership import intact
from tuner_under_test.history import dayfile, daystore, recording

DAY = 86400


class RecordingOwnershipTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_automatic_weight.DeviceLearnTests.setUp
    asyncTearDown = test_automatic_weight.DeviceLearnTests.asyncTearDown
    configuration = test_automatic_weight.DeviceLearnTests.configuration

    def recorded_view(self):
        """Two recorded days (the first finished), and a learning view of them."""
        self.configuration()
        days = self.runtime._days
        start = float(int(self.now)) - DAY
        days.append("a", dayfile.Change(start, {0: 20}))
        days.append("a", dayfile.Change(start + DAY, {0: 21}))
        days.flush(now=start + DAY + 1)
        self.finished = daystore.date_of(start)
        return recording._history_view(self.runtime, "a"), start

    def test_appends_flushes_and_finished_days_remain_owned(self):
        view, start = self.recorded_view()
        days = self.runtime._days
        days.append("a", dayfile.Change(start + DAY + 5, {0: 22}))
        days.flush(now=start + 2 * DAY + 1)  # The live day is finished too.
        self.assertTrue(intact(view, self.runtime, ["a"]))

    def test_a_confirmation_after_the_view_fills_only_unguessed_time(self):
        view, start = self.recorded_view()
        span = dayfile.GuessSpan(start + DAY + 9, start + DAY, start + DAY + 8, "present", 18, 2)
        self.runtime._days.append("a", span)
        self.assertTrue(intact(view, self.runtime, ["a"]))

    def test_a_rewritten_finished_day_is_not_an_append(self):
        view, _start = self.recorded_view()
        days = self.runtime._days
        days.replace("a", self.finished, days.day("a", self.finished))  # Thinning rewrote it.
        self.assertFalse(intact(view, self.runtime, ["a"]))

    def test_a_deleted_day_is_not_an_append(self):
        view, _start = self.recorded_view()
        self.runtime._days.delete("a", [self.finished])  # Retention or Trim now.
        self.assertFalse(intact(view, self.runtime, ["a"]))

    def test_cleared_or_removed_radars_are_not_an_append(self):
        view, _start = self.recorded_view()
        self.runtime.clear_samples("a")
        self.assertFalse(intact(view, self.runtime, ["a"]))
        view, _start = self.recorded_view()
        del self.runtime.data["devices"]["a"]
        self.assertFalse(intact(view, self.runtime, ["a"]))

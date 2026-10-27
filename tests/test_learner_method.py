"""The panel judges saved results against the learner the backend actually runs."""

import unittest

import test_tuner as harness
from tuner_under_test.calibration.constants import METHOD


class LearnerMethodTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown

    def test_snapshot_exposes_the_current_learner_method(self):
        self.assertEqual(self.runtime.snapshot()["learner_method"], METHOD)

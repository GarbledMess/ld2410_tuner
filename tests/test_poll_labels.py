"""The poll finds each span's human label through the indexed reader, never a scan of every
label, and never with a stale answer after any kind of label change."""

import random
import time
import unittest

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.const import HISTORY_KEYS
from tuner_under_test.history import label_lookup, labels, live


def _device(rng):
    def periods(count):
        found, moment = [], 1000.0
        for _ in range(count):
            moment += rng.uniform(-50, 200)  # Overlapping periods: the later one wins.
            found.append(
                {
                    "start": moment,
                    "end": moment + rng.choice((-1, 0, 30, 300)),
                    "state": rng.choice(("present", "not_present", "unknown")),
                }
            )
        return found

    device = {"history_labels": periods(60), "group_labels": periods(20)}
    if rng.random() < 0.7:
        device["training_label_start"] = rng.uniform(1000, 9000)
        device["training_expires_at"] = rng.choice((None, rng.uniform(9000, 12000)))
        device["training_state"] = rng.choice(("present", "not_present", "unknown"))
    return device


@pytest.mark.parametrize("seed", range(20))
def test_the_indexed_reader_answers_as_the_scan_does(seed):
    rng = random.Random(seed)
    device = _device(rng)
    reader = label_lookup._history_label_reader(device, cache={})
    edges = {float(x) for label in device["history_labels"] for x in (label["start"], label["end"])}
    moments = [rng.uniform(900, 13000) for _ in range(300)] + sorted(edges)
    for moment in moments:
        assert (reader(moment) or "unknown") == labels._history_label_at(device, moment)


def test_a_replaced_or_grown_label_list_is_never_read_stale():
    device = {"history_labels": [{"start": 0, "end": 10, "state": "present"}]}
    cache = {}
    assert label_lookup._history_label_reader(device, cache=cache)(5) == "present"
    # Installed wholesale without a revision (as storage maintenance may), then appended to.
    device["history_labels"] = [{"start": 0, "end": 10, "state": "not_present"}]
    assert label_lookup._history_label_reader(device, cache=cache)(5) == "not_present"
    device["history_labels"].append({"start": 4, "end": 6, "state": "present"})
    assert label_lookup._history_label_reader(device, cache=cache)(5) == "present"


class PollLabelTests(unittest.TestCase):
    setUp = test_tuner.RuntimeTests.setUp

    def test_the_next_poll_after_a_label_edit_counts_under_the_new_label(self):
        runtime = self.runtime
        device = runtime.data["devices"]["a"]
        now = time.time()
        values = dict.fromkeys(HISTORY_KEYS, 30.0)
        guess = ("present", 0, 0)
        device["history_labels"] = [{"start": now - 100, "end": now + 100, "state": "present"}]
        live.poll(runtime, "a", device, values, now, guess)
        live.poll(runtime, "a", device, {**values, "g0_move": 31.0}, now + 5, guess)
        present = device["histograms"]["g0_move"]["present"][30]
        assert present > 0
        device["history_labels"] = [{"start": now - 100, "end": now + 100, "state": "not_present"}]
        device["label_revision"] = device.get("label_revision", 0) + 1
        live.poll(runtime, "a", device, {**values, "g0_move": 32.0}, now + 10, guess)
        assert device["histograms"]["g0_move"]["present"][30] == present
        assert device["histograms"]["g0_move"]["not_present"][31] > 0

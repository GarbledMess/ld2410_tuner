"""Recorded guesses a radar's human labels prove carry no information are not evidence: they
abstain. Labels of the radar, of its zones and of its rooms all count as its human truth."""

import unittest

import test_tuner  # noqa: F401
from small_views import view
from tuner_under_test.engine.hybrid.quality import recorded_guesses
from tuner_under_test.history import guess_check, guesses
from tuner_under_test.rooms import evidence

H = 3600.0
REF = guesses.REFERENCE


def truth(*spans):
    return [(start * H, end * H, state) for start, end, state in spans]


def run(start, end, state, level=13, basis=REF):
    return (start * H, end * H, state, level, basis)


# Ten labelled hours present, then seven labelled hours empty.
LABELLED = truth((0, 10, "present"), (10, 17, "not_present"))


class DiscardedTests(unittest.TestCase):
    def test_a_guess_that_says_present_whatever_the_labels_say_is_discarded(self):
        runs = [run(0, 17, "present")]
        self.assertEqual(guess_check.discarded(runs, LABELLED), {("present", 13, REF)})

    def test_a_guess_that_follows_the_labels_is_kept(self):
        runs = [run(0, 10, "present"), run(10, 17, "not_present")]
        self.assertEqual(guess_check.discarded(runs, LABELLED), set())

    def test_without_labels_of_the_other_state_nothing_is_proven(self):
        runs = [run(0, 10, "present")]
        self.assertEqual(guess_check.discarded(runs, truth((0, 10, "present"))), set())
        self.assertEqual(guess_check.discarded(runs, []), set())

    def test_without_labels_of_the_guessed_state_only_long_proof_discards(self):
        # An hour of contradiction is not proof; seven hours are.
        self.assertEqual(
            guess_check.discarded([run(0, 1, "present")], truth((0, 1, "not_present"))), set()
        )
        self.assertEqual(
            guess_check.discarded([run(0, 7, "present")], truth((0, 7, "not_present"))),
            {("present", 13, REF)},
        )

    def test_a_guess_mostly_right_in_empty_time_is_kept(self):
        # Present through the presence, and through 40 % of each empty hour.
        runs = [run(0, 10, "present")]
        for hour in range(10, 17):
            runs += [run(hour, hour + 0.4, "present"), run(hour + 0.4, hour + 1, "not_present")]
        self.assertEqual(guess_check.discarded(runs, LABELLED), set())

    def test_each_level_is_judged_against_its_own_guessed_time(self):
        # Level 15 follows the labels; level 18 says present whenever it guesses.
        runs = [
            run(0, 5, "present", 15),
            run(5, 10, "present", 18),
            run(10, 11, "not_present", 15),
            run(11, 17, "present", 18),
        ]
        self.assertEqual(guess_check.discarded(runs, LABELLED), {("present", 18, REF)})

    def test_an_empty_guess_through_labelled_presence_is_discarded(self):
        runs = [run(0, 17, "not_present", 15)]
        self.assertEqual(guess_check.discarded(runs, LABELLED), {("not_present", 15, REF)})

    def test_a_guess_with_little_evidence_is_kept_however_it_reads(self):
        # Minutes of labels prove nothing either way.
        right = [run(0, 0.1, "present"), run(10, 10.1, "not_present")]
        wrong = [run(0, 0.1, "not_present"), run(10, 10.1, "present")]
        self.assertEqual(guess_check.discarded(right, LABELLED), set())
        self.assertEqual(guess_check.discarded(wrong, LABELLED), set())

    def test_sources_are_judged_apart(self):
        # One radar records one guess at a time: an external source around the radar's own.
        external = guesses.EXTERNAL
        runs = [run(0, 5, "present", 13, external), run(5, 10, "present", 13)]
        runs.append(run(10, 17, "present", 13, external))
        self.assertEqual(guess_check.discarded(runs, LABELLED), {("present", 13, guesses.EXTERNAL)})


def _labelled(state, start, end):
    return {"start": start * H, "end": end * H, "state": state, "source": "manual"}


def _room(radar_labels=(), groups=None, room_labels=None, members=("any-name",)):
    """A recorded radar whose snapshot puts it in ``groups`` with ``room_labels``."""
    result = view([], list(radar_labels), [run(0, 17, "present")])
    result.data["room_snapshot"] = {
        "device_id": "any-name",
        "own": [key for key, item in groups.items() if "any-name" in item["device_ids"]],
        "groups": groups,
        "room_labels": room_labels or {},
        "members": {key: {"history_labels": []} for key in members},
    }
    return result


AREA = {"kind": "area", "area_id": "lounge"}
ZONE = {"kind": "zone", "area_id": "lounge"}


class HumanTruthTests(unittest.TestCase):
    def test_a_radars_own_labels_are_its_truth(self):
        found = guess_check.human_truth(view([], [_labelled("not_present", 0, 7)]), "any-name")
        self.assertEqual(found, [(0, 7 * H, "not_present")])

    def test_a_live_label_reaches_as_far_as_the_recording(self):
        source = view([], [])
        source._history_until = 5 * H
        device = source.data["devices"]["any-name"]
        device.update(training_label_start=2 * H, training_state="not_present")
        self.assertEqual(
            guess_check.human_truth(source, "any-name"), [(2 * H, 5 * H, "not_present")]
        )

    def test_an_empty_room_is_empty_for_each_of_its_radars(self):
        groups = {"area:lounge": {**AREA, "device_ids": ["any-name", "other"]}}
        labels = {"area:lounge": [_labelled("not_present", 0, 7), _labelled("present", 7, 9)]}
        found = guess_check.human_truth(
            _room(groups=groups, room_labels=labels, members=("any-name", "other")), "any-name"
        )
        # Presence of a shared room is not presence this radar must see.
        self.assertEqual(found, [(0, 7 * H, "not_present")])

    def test_a_room_with_one_radar_is_that_radar(self):
        groups = {"area:lounge": {**AREA, "device_ids": ["any-name"]}}
        labels = {"area:lounge": [_labelled("present", 0, 3), _labelled("not_present", 3, 7)]}
        found = guess_check.human_truth(_room(groups=groups, room_labels=labels), "any-name")
        self.assertEqual(found, [(0, 3 * H, "present"), (3 * H, 7 * H, "not_present")])

    def test_an_empty_zone_and_the_room_around_it_are_empty_for_its_radar(self):
        groups = {
            "zone:sofa": {**ZONE, "device_ids": ["any-name", "other"]},
            "area:lounge": {**AREA, "device_ids": ["other", "third"]},
        }
        labels = {
            "zone:sofa": [_labelled("not_present", 0, 2)],
            "area:lounge": [_labelled("not_present", 5, 7)],
        }
        found = guess_check.human_truth(
            _room(groups=groups, room_labels=labels, members=("any-name", "other")), "any-name"
        )
        self.assertEqual(found, [(0, 2 * H, "not_present"), (5 * H, 7 * H, "not_present")])

    def test_the_radars_own_label_wins_over_its_rooms(self):
        groups = {"area:lounge": {**AREA, "device_ids": ["any-name", "other"]}}
        labels = {"area:lounge": [_labelled("not_present", 0, 7)]}
        found = guess_check.human_truth(
            _room([_labelled("present", 2, 3)], groups, labels, ("any-name", "other")),
            "any-name",
        )
        self.assertEqual(
            found,
            [(0, 2 * H, "not_present"), (2 * H, 3 * H, "present"), (3 * H, 7 * H, "not_present")],
        )


class AbstainTests(unittest.TestCase):
    def _always_present(self):
        groups = {"area:lounge": {**AREA, "device_ids": ["any-name"]}}
        labels = {"area:lounge": [_labelled("present", 0, 10), _labelled("not_present", 10, 17)]}
        return _room(groups=groups, room_labels=labels)

    def test_every_read_of_a_radars_guesses_marks_the_discarded_ones(self):
        source = self._always_present()
        for found in (
            guesses.of(source, "any-name"),
            recorded_guesses(source, "any-name", 0, 17 * H),
        ):
            self.assertEqual([r[4] for r in found.runs], [guesses.DISCARDED])
            self.assertIs(found.at(H), guesses.ABSTAIN)
            self.assertFalse(found)
            self.assertEqual(found.cells(), [])

    def test_a_discarded_guess_is_no_evidence(self):
        found = guesses.evidence(self._always_present(), "any-name")
        self.assertIs(found.at(H), guesses.ABSTAIN)
        self.assertFalse(found)

    def test_an_abstaining_member_neither_vetoes_nor_votes(self):
        empty = ("not_present", "automatic", 0.9)
        present = ("present", "automatic", 0.9)
        self.assertEqual(evidence.combine([guesses.ABSTAIN, empty]), empty)
        self.assertEqual(evidence.combine([guesses.ABSTAIN, present]), present)
        self.assertIsNone(evidence.combine([guesses.ABSTAIN]))
        self.assertIsNone(evidence.combine([guesses.ABSTAIN, guesses.ABSTAIN]))

    def test_a_member_with_no_guess_still_vetoes_empty(self):
        self.assertIsNone(evidence.combine([None, ("not_present", "automatic", 0.9)]))


if __name__ == "__main__":
    unittest.main()

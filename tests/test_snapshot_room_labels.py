"""Room labels reach the panel only when they change.

Every 3 s poll resent every room's whole label history: on the real-shaped home that was
447 KB of a 562 KB snapshot. The panel now sends the label tokens it holds; a room whose labels
are unchanged answers with its token alone. The token is the labels' content, so no writer
(manual edits, sensor extensions that bump no revision, maintenance) can leave it stale.
"""

import json

import real_shape
from tuner_under_test.rooms import room_labels

ROOM = "area:kitchen"


def _tokens(snapshot):
    return {key: group["labels_token"] for key, group in snapshot["rooms"]["groups"].items()}


def _without_labels(snapshot):
    copied = json.loads(json.dumps(snapshot))
    for group in copied["rooms"]["groups"].values():
        group.pop("labels", None)
        group.pop("labels_unchanged", None)
    copied.pop("workers", None)  # Live worker timings move between two calls.
    return copied


def test_known_labels_are_not_resent_and_everything_else_is(play):
    async def scenario(home):
        full = await home.ws("snapshot")
        groups = full["rooms"]["groups"]
        assert all("labels" in group and group["labels_token"] for group in groups.values())
        assert any(group["labels"] for group in groups.values())
        known = await home.ws("snapshot", room_label_tokens=_tokens(full))
        for key, group in known["rooms"]["groups"].items():
            assert "labels" not in group
            assert group["labels_unchanged"] is True
            assert group["labels_token"] == groups[key]["labels_token"]
        assert _without_labels(known) == _without_labels(full)
        assert len(json.dumps(known)) < len(json.dumps(full)) / 2

    play(scenario)


def test_any_label_change_resends_that_room_alone(play):
    async def scenario(home):
        full = await home.ws("snapshot")
        tokens = _tokens(full)
        labels = full["rooms"]["groups"][ROOM]["labels"]
        low = real_shape.NOW - 3 * 3600
        await home.ws("label_room_range", group_id=ROOM, start=low, end=low + 600, state="present")
        edited = await home.ws("snapshot", room_label_tokens=tokens)
        changed = edited["rooms"]["groups"][ROOM]
        assert changed["labels"] != labels and changed["labels_token"] != tokens[ROOM]
        others = {k: g for k, g in edited["rooms"]["groups"].items() if k != ROOM}
        assert all(group.get("labels_unchanged") for group in others.values())

        # A sensor label growing bumps no revision; the token still moves.
        tokens = _tokens(edited)
        room_labels.write_sensor_range(
            home.runtime, ROOM, low + 900, low + 960, "present", 0.8, bump=True
        )
        grown_from = await home.ws("snapshot", room_label_tokens=tokens)
        tokens = _tokens(grown_from)
        room_labels.write_sensor_range(
            home.runtime, ROOM, low + 960, low + 1020, "present", 0.8, bump=False
        )
        grown = await home.ws("snapshot", room_label_tokens=tokens)
        group = grown["rooms"]["groups"][ROOM]
        assert "labels" in group and group["labels_token"] != tokens[ROOM]
        grown_label = [x for x in group["labels"] if x.get("source") == "sensor"]
        assert any(x["start"] == low + 900 and x["end"] == low + 1020 for x in grown_label)

    play(scenario)


def test_an_unknown_or_missing_token_always_gets_the_labels(play):
    async def scenario(home):
        full = await home.ws("snapshot")
        stale = dict.fromkeys(_tokens(full), "not-a-token")
        answered = await home.ws("snapshot", room_label_tokens=stale)
        assert all("labels" in group for group in answered["rooms"]["groups"].values())

    play(scenario)


def test_a_label_with_an_unhashable_value_still_has_an_exact_token():
    from tuner_under_test.presentation import snapshots

    first = [{"start": 1.0, "end": 2.0, "state": "present", "extra": [1]}]
    second = [{"start": 1.0, "end": 2.0, "state": "present", "extra": [2]}]
    assert snapshots._labels_token(first) == snapshots._labels_token(first)
    assert snapshots._labels_token(first) != snapshots._labels_token(second)

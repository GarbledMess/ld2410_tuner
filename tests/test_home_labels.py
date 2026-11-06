"""Labelling and relabelling on the real-shaped home, as the panel does."""

import pytest
import real_home
from real_home import WebsocketError
from real_shape import DAY, TODAY

RADAR = "hall_1"
ROOM = "area:kitchen"


async def _chart_labels(home, radar, start):
    graph = await home.ws(
        "history_series_multi",
        device_id=radar,
        keys=["g1_move"],
        hours=24,
        end=start + DAY,
    )
    return graph["labels"]


def test_label_shows_on_the_graph_counts_its_readings_and_relabel_guards_a_stale_panel(play):
    async def scenario(home):
        start = TODAY - DAY
        low, high = start + 1234.5, start + 1834.25  # Off any recording grid, ten minutes long.
        before = (await home.ws("snapshot"))["devices"][RADAR]["sample_counts"]["g1_move"]

        saved = await home.ws(
            "label_history", device_id=RADAR, start=low, end=high, state="present"
        )
        await home.settle()
        assert any(
            (label["start"], label["end"], label["state"]) == (low, high, "present")
            for label in saved["history_labels"]
        )
        shown = await _chart_labels(home, RADAR, start)
        assert any(
            (label["start"], label["end"], label["state"]) == (low, high, "present")
            for label in shown
        )
        after = (await home.ws("snapshot"))["devices"][RADAR]["sample_counts"]["g1_move"]
        # Ten minutes is 100 six-second units of the radar's recorded time.
        assert after["present"] > before["present"]
        labelled = sum(
            label["end"] - label["start"]
            for label in saved["history_labels"]
            if label["state"] == "present"
        )
        # Within 1%: a few rows end early, and a stop in coverage is not recorded time.
        assert after["present"] == pytest.approx(labelled / 6, rel=0.01)

        # The same radar relabelled from a panel that has seen the change, then a stale one.
        revision = home.runtime.data["devices"][RADAR]["label_revision"]
        await home.ws(
            "edit_history_label",
            device_id=RADAR,
            label_start=low,
            label_end=high,
            start=low,
            end=high + 60,
            state="unknown",
            revision=revision,
        )
        await home.settle()
        shown = await _chart_labels(home, RADAR, start)
        assert any(
            (label["start"], label["end"], label["state"]) == (low, high + 60, "unknown")
            for label in shown
        )
        assert not any(label["start"] == low and label["end"] == high for label in shown)
        with pytest.raises(WebsocketError, match="labels changed"):
            await home.ws(
                "edit_history_label",
                device_id=RADAR,
                label_start=low,
                label_end=high + 60,
                start=low,
                end=high,
                state="unlabelled",
                revision=revision,
            )

    play(scenario)


def test_labels_reject_what_could_not_be_true(play):
    async def scenario(home):
        with pytest.raises(WebsocketError):
            await home.ws(
                "label_history", device_id=RADAR, start=TODAY, end=TODAY - 60, state="present"
            )
        with pytest.raises(WebsocketError):
            await home.ws(
                "label_history", device_id="no_such_radar", start=1.0, end=2.0, state="present"
            )

    play(scenario)


def test_a_room_label_reaches_every_radar_and_leaves_the_sensor_labels_alone(play):
    async def scenario(home):
        data = home.runtime.data
        sensor_before = [x for x in data["room_labels"][ROOM] if x["source"] == "sensor"]
        low, high = TODAY - DAY + 3601.5, TODAY - DAY + 7322.75

        saved = await home.ws(
            "label_room_range", group_id=ROOM, start=low, end=high, state="present"
        )
        await home.settle()
        assert saved["revision"] == data["room_label_revision"][ROOM]
        assert any(
            (x["start"], x["end"], x["source"]) == (low, high, "manual")
            for x in saved["room_labels"]
        )
        assert [x for x in data["room_labels"][ROOM] if x["source"] == "sensor"] == sensor_before

        # A single-radar room's label is the radar's; a two-radar room's stays the room's.
        graph = await home.ws(
            "room_series",
            device_ids=["kitchen_1", "kitchen_2"],
            keys=["g1_move"],
            hours=24,
            end=TODAY,
        )
        assert set(graph["charts"]) == {"kitchen_1", "kitchen_2"}

        with pytest.raises(WebsocketError, match="labels changed"):
            await home.ws(
                "edit_room_label",
                group_id=ROOM,
                label_start=low,
                label_end=high,
                start=low,
                end=high,
                state="unlabelled",
                revision=saved["revision"] - 1,
            )
        cleared = await home.ws(
            "edit_room_label",
            group_id=ROOM,
            label_start=low,
            label_end=high,
            start=low,
            end=high,
            state="unlabelled",
            revision=saved["revision"],
        )
        assert not any(x["start"] == low for x in cleared["room_labels"])
        assert [x for x in data["room_labels"][ROOM] if x["source"] == "sensor"] == sensor_before

    play(scenario)


def test_labels_and_settings_saved_at_shutdown_are_there_after_a_restart(play, tmp_path):
    low, high = TODAY - DAY + 100.5, TODAY - DAY + 200.25

    async def change_things(home):
        await home.ws("label_history", device_id=RADAR, start=low, end=high, state="present")
        await home.ws("label_room_range", group_id=ROOM, start=low, end=high, state="not_present")
        await home.ws("configure_automatic_weight", weight=0.25)
        # Home Assistant unloads straight away: nothing has had its delayed save.

    home = play(change_things)

    async def read_back(restarted):
        data = restarted.runtime.data
        assert any(x["start"] == low for x in data["devices"][RADAR]["history_labels"])
        assert any(x["start"] == low for x in data["room_labels"][ROOM])
        snapshot = await restarted.ws("snapshot")
        assert snapshot["learning_schedule"]["automatic_weight"] == 0.25

    second = real_home.Home(home.config)
    with real_home.worker_on_transport(second):
        real_home.run_on_clock(second.clock, lambda: real_home.lifetime(second, read_back))

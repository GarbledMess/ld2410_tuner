"""Every limit between the panel and the worker has to admit a real home's metadata and history.

A green suite on small fixtures once hid a metadata limit that refused every graph and Learn on
a real install. These walk the whole way to the worker with the full sixty days and all twelve
radars, stopping where the numerical work would begin.
"""

import asyncio

import pytest
import real_shape
from real_shape import HISTORY_KEYS, TODAY
from tuner_under_test.worker import bridge, calculations, codec, protocol


class ReachedWorker(Exception):
    pass


def _stop_in_worker(home):
    """The worker admits the request, expands its manifest and opens every view; then stops."""
    reached = []

    async def respond(request):
        from tuner_under_test.worker.manifest import expand

        expanded = expand(request) if "manifest" in request else request
        parameters = codec.decode(expanded.get("parameters", expanded))
        shared = calculations.JobHistory()
        views = {
            key: calculations.restore(value, shared) for key, value in expanded["views"].items()
        }
        reached.append((request["operation"], views, parameters))
        raise ReachedWorker(request["operation"])

    home.transport.respond = respond
    return reached


def _nodes(value):
    if isinstance(value, dict):
        return 1 + sum(_nodes(item) + 1 for item in value.values())
    if isinstance(value, list):
        return 1 + sum(_nodes(item) for item in value)
    return 1


def test_the_saved_home_is_the_size_of_a_real_one():
    data = real_shape.saved_data()
    assert 60_000 <= _nodes(data) <= 80_000
    labels = sum(
        label["source"] == "sensor" for found in data["room_labels"].values() for label in found
    )
    assert labels / real_shape.SENSOR_LABEL_DAYS == pytest.approx(248, abs=4)


def test_graph_and_learn_of_every_size_are_accepted_by_every_limit_on_the_way_to_the_worker(play):
    async def scenario(home):
        bridge.preflight(home.runtime)
        reached = _stop_in_worker(home)
        radar, room = "office_1", "living_room_1"

        # A radar's Learn with the whole retained history.
        await home.ws("start_learning", device_id=radar)
        await asyncio.gather(*home.runtime._learning_jobs.values(), return_exceptions=True)
        report = home.runtime.data["devices"][radar]["learning_job"]
        assert report["error"] == "learn"
        # A two-radar room with all of it.
        await home.ws("start_learning", device_id=room)
        await asyncio.gather(*home.runtime._learning_jobs.values(), return_exceptions=True)
        report = home.runtime.data["devices"][room]["learning_job"]
        assert report["error"] == "room_fit"
        assert [item[0] for item in reached if item[0] != "references"] == ["learn", "room_fit"]

        # The long graph the panel can ask for: every retained day of one gate.
        home.transport.respond = None
        graph = await home.ws(
            "history_series_multi",
            device_id=radar,
            keys=list(HISTORY_KEYS),
            hours=real_shape.RETAINED_DAYS * 24,
            end=TODAY,
        )
        assert graph["series"]["g1_move"]["points"]
        graph = await home.ws(
            "room_series",
            device_ids=[radar, room, "living_room_2"],
            keys=["g1_move", "g1_still"],
            hours=48,
            end=TODAY,
        )
        assert not graph["errors"]

    play(scenario, whole=True)


def test_reference_evidence_at_its_cap_on_every_radar_does_not_lock_the_home_out(play):
    """Each radar keeps at most 24 periods per source and state. Those limits and the worker's
    metadata limits have to agree, or a home that labels diligently loses every graph."""

    def full_profiles(home):
        import json

        path = home.config / real_shape.STORE_FOLDER / f"{real_shape.DOMAIN}.data"
        saved = json.loads(path.read_text(encoding="utf8"))
        for radar, device in saved["data"]["devices"].items():
            device["reference_profile"] = real_shape.reference_profile_at_cap(radar)
        path.write_text(json.dumps(saved), encoding="utf8")

    async def scenario(home):
        bridge.preflight(home.runtime)
        graph = await home.ws(
            "history_series_multi", device_id="hall_1", keys=["g1_move"], hours=24, end=TODAY
        )
        assert graph["series"]["g1_move"]["points"]

    play(scenario, prepare=full_profiles)


def test_metadata_past_any_sane_size_is_still_refused(play):
    async def scenario(home):
        home.runtime.data["room_labels"]["area:office"] = [{"start": 1.0}] * (
            bridge.METADATA_NODES + 1
        )
        with pytest.raises(protocol.ProtocolRejected):
            bridge.preflight(home.runtime)
        home.runtime.data["room_labels"]["area:office"] = []

    play(scenario)

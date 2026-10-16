"""Room attribution uses observed sustain and quiet duration for event histories."""

import math

import test_tuner  # noqa: F401
from test_rooms import member
from tuner_under_test.rooms import attribution


def vector(value):
    return (value,) + (None,) * 17


def fixture(present_end=600, gap=False, quiet=None):
    item = member("any-name", [], [])
    view = item["view"]
    stretches = [([(0, vector(40))], present_end), ([(600, vector(0))], 1200)]
    if gap:
        stretches = [([(0, vector(40))], 0.1), ([(20, vector(40))], 20.1), stretches[-1]]
    if quiet is not None:
        stretches[-1] = quiet
    for events, end in stretches:
        test_tuner.recorded(view, "any-name", events, end)
    saved = {
        "device_id": "any-name",
        "own": ["zone"],
        "groups": {"zone": {"device_ids": ["any-name"], "kind": "zone"}},
        "members": {"any-name": {}},
        "room_labels": {
            "zone": [
                {"start": 0, "end": 600, "state": "present", "source": "manual"},
                {"start": 600, "end": 1200, "state": "not_present", "source": "manual"},
            ]
        },
    }
    return view, saved


def test_stable_native_occupied_span_is_sustained_above_duration_quiet_baseline():
    view, saved = fixture()
    result = attribution.radar_attribution(view, "any-name", saved)
    assert result["found"]["zone"] == [
        ((0, math.nextafter(600, -math.inf)), {"state": "yes", "confidence": 1, "inferred": True})
    ]


def test_short_spikes_separated_by_outages_are_not_sustained():
    view, saved = fixture(gap=True)
    result = attribution.radar_attribution(view, "any-name", saved)
    assert len(result["found"]["zone"]) == 2
    assert all(item[1]["state"] == "no" for item in result["found"]["zone"])


def test_dense_quiet_delivery_retains_exact_minimum_reference_support():
    events = [(600 + i / 2, (0.01 + (i % 2) * 0.01,) + (None,) * 17) for i in range(240)]
    view, saved = fixture(quiet=(events, 720))
    result = attribution.radar_attribution(view, "any-name", saved)
    assert result["found"]["zone"][0][1]["state"] == "yes"
    assert result["found"]["zone"][0][1]["confidence"] == 0.2


def test_seen_by_native_episode_excludes_the_cell_after_it():
    from tuner_under_test.rooms import seen_by

    view, saved = fixture()
    saved["room_labels"]["zone"] = saved["room_labels"]["zone"][:1]  # No truth after 600.
    view.data["room_snapshot"] = saved
    episodes = seen_by._member_episodes(view, "any-name", "zone")
    assert len(episodes) == 1
    (start, end), _result, estimated = episodes[0]
    assert (start, estimated) == (0, False)
    assert end < 600


def test_native_episode_attribution_never_claims_the_instant_after_it():
    import math

    view, saved = fixture()
    found = attribution.radar_attribution(view, "any-name", saved)["found"]["zone"]
    starts = [span[0] for span, _ in found]
    assert attribution._attributed(found, starts, 599.999)["state"] == "yes"
    assert attribution._attributed(found, starts, 600) is None
    assert math.nextafter(found[0][0][1], math.inf) == 600

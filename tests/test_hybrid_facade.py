"""One hybrid facade behind every device, room, adaptive and comparison surface.

Histories are synthetic day files read through the production worker reader, so
evidence, quality, Seen-by attribution and row sources are the production ones.
"""

from types import SimpleNamespace

import pytest
import test_tuner  # noqa: F401
from hybrid_scenarios import assert_device_contract, assert_room_contract
from tuner_under_test.calibration.constants import ROOM_METHOD
from tuner_under_test.engine.hybrid import engine, facade, search
from tuner_under_test.history import dayfile
from tuner_under_test.worker import history
from tuner_under_test.worker.calculations import ReadOnlyDays

KEY = "g0_move"
QUIET = 25  # Occupied but quieter than every training presence.


def episodes(*spec, seconds=120):
    """``(state, energy)`` episodes of ``seconds`` each, as 6-second values and labels."""
    values, labels = [], []
    for i, (state, energy) in enumerate(spec):
        values += [energy] * (seconds // 6)
        labels.append({"start": i * seconds, "end": (i + 1) * seconds, "state": state})
    return values, labels


def alternating(count, present=60, empty=10, last_present=None):
    spec = [("present" if i % 2 == 0 else "not_present") for i in range(count)]
    energies = [present if s == "present" else empty for s in spec]
    if last_present is not None:
        energies[max(i for i, s in enumerate(spec) if s == "present")] = last_present
    return episodes(*zip(spec, energies, strict=True))


def worker_view(tmp_path, device_id, values, labels=()):
    """A worker-restored view over one synthetic recorded day of ``values``."""
    builder = dayfile.DayBuilder(0, 86400)
    for i, value in enumerate(values):
        builder.add(dayfile.Change(i * 6, {0: value, **dict.fromkeys(range(1, 18), 255)}))
    end = len(values) * 6
    builder.add(dayfile.End(end))
    path = tmp_path / f"{device_id}.day"
    path.write_bytes(dayfile.encode_day(builder.day()))
    entry = {
        "device_id": device_id,
        "date": "1970-01-01",
        "path": str(path),
        "identity": history._identity(path.stat()),
        "late": [],
        "persistent": False,
    }
    budgets = {"max_file_bytes": None, "max_raw_bytes": 1 << 30, "max_rows": None}
    days = ReadOnlyDays({"days": [entry], "budgets": {**budgets, "max_guesses": None}})
    device = {"history_labels": list(labels), "training_state": "unknown"}
    return SimpleNamespace(
        data={"devices": {device_id: device}, "room_snapshot": None},
        hass=None,
        _days=days,
        _history_until=end,
        _attribution_until=end,
    )


def member(tmp_path, device_id, values, labels=(), threshold=90):
    view = worker_view(tmp_path, device_id, values, labels)
    return {"view": view, "thresholds": {KEY: threshold}, "timing": {}}


def room(group_id, labels):
    return {
        "group_id": group_id,
        "groups": {},
        "room_labels": {group_id: [{**label, "source": "manual"} for label in labels]},
    }


@pytest.fixture
def fits(monkeypatch):
    """Every engine fit, recorded while still running the real engine."""
    calls, original = [], engine.fit

    def spy(problem, **kwargs):
        result = original(problem, **kwargs)
        calls.append((problem, result))
        return result

    monkeypatch.setattr(engine, "fit", spy)
    return calls


def device_fit(view, weight=0.0):
    return facade.fit(view, "r", [KEY], {KEY: 90}, None, weight)


def thresholds(report):
    return {key: proposal["threshold"] for key, proposal in report["proposals"].items()}


def test_radar_room_and_single_zone_set_share_results(tmp_path):
    values, labels = alternating(10)
    item = member(tmp_path, "r", values, labels)
    device = device_fit(item["view"])
    single = facade.room_fit({"r": item}, 0, 1200)
    linked = facade.fit_set({"zone": {"r": item}}, 0, 1200)
    assert thresholds(device) == single["thresholds"]["r"] == linked["thresholds"]["r"]
    assert thresholds(device) != {KEY: 90}
    assert linked.pop("groups") == {
        "zone": {
            "before": single["before"],
            "after": single["after"],
            "cost_before": single["cost_before"],
            "cost_after": single["cost_after"],
        }
    }
    assert linked == single
    assert device["base_cost"] == single["cost_after"]
    assert device["training"]["duration"]["error_cost"] == single["after"]["room"]["error_cost"]


def test_adaptive_runs_two_training_searches_then_selected_full_refit(tmp_path, fits):
    values, labels = alternating(10, last_present=QUIET)
    report = device_fit(worker_view(tmp_path, "r", values, labels), weight=0.5)
    policy = report["adaptive_policy"]
    assert policy["cutoff"] == 960
    assert set(policy["candidates"]) == {"reference", "corrected_positive"}
    training, final = fits[:2], fits[2:]
    assert [p.policy.reference for p, _ in training] == [True, False]
    assert all(p.end <= policy["cutoff"] for p, _ in training)
    assert len(final) == 1
    problem, result = final[0]
    assert problem.end == 1200
    assert problem.policy.reference == (policy["selected"] == "reference")
    assert thresholds(report) == {KEY: result.thresholds["r"][0]}


def test_room_adaptive_runs_two_training_searches_then_selected_refit(tmp_path, fits):
    from tuner_under_test.rooms import adaptive

    values, labels = alternating(10, last_present=QUIET)
    groups = {"zone": {"r": member(tmp_path, "r", values)}}
    rooms = {"zone": room("zone", labels)}
    contexts = adaptive.collect_contexts(groups, 0, 1200, rooms)
    result = facade.fit_set(groups, 0, 1200, rooms, 0.5, quality_contexts=contexts)
    selection = result["automatic_evidence"]["selection"]
    assert selection["cutoff"] == 960
    assert [p.policy.reference for p, _ in fits] == [
        True,
        False,
        selection["policy"] == "reference",
    ]
    assert [p.end for p, _ in fits] == [960, 960, 1200]
    assert result["validation_scope"] == "later_explicit_episodes"


def test_zero_weight_identical_policies_reuse_search(tmp_path, fits):
    values, labels = alternating(10, last_present=QUIET)
    report = device_fit(worker_view(tmp_path, "r", values, labels), weight=0.0)
    candidates = report["adaptive_policy"]["candidates"]
    assert candidates["reference"] == candidates["corrected_positive"]
    assert len(fits) == 2
    assert fits[0][0].end <= 960 and fits[1][0].end == 1200


def test_held_policy_scoring_uses_common_episode_cost(tmp_path):
    values, labels = alternating(10, last_present=QUIET)
    view = worker_view(tmp_path, "r", values, labels)
    policy = device_fit(view, weight=0.5)["adaptive_policy"]
    for candidate in policy["candidates"].values():
        score = candidate["score"]
        later = facade.assess(view, "r", candidate["thresholds"], start=policy["cutoff"])
        duration = later["human"]["duration"]
        assert score["cost"] == pytest.approx(duration["error_cost"])
        assert score["cost"] > 0
        assert score["mostly_missed_episodes"] == later["human"]["mostly_missed_episodes"] == 1
        assert score["missed_seconds"] == pytest.approx(duration["missed_seconds"])
        assert score["present_seconds"] == duration["present_seconds"] == 120


def test_skipped_zone_keeps_exclusive_radars_current(tmp_path, fits):
    values, labels = alternating(6)
    shared = member(tmp_path, "shared", values)
    only = member(tmp_path, "only", values, threshold=77)
    groups = {"good": {"shared": shared}, "presence": {"shared": shared, "only": only}}
    present = [label for label in labels if label["state"] == "present"]
    rooms = {"good": room("good", labels), "presence": room("presence", present)}
    result = facade.fit_set(groups, 0, 720, rooms)
    reason = facade.measure(groups["presence"], {"shared": {KEY: 90}, "only": {KEY: 77}}, 0, 720)
    assert result["groups"]["presence"] == {"skipped": reason["room"]["reason"]}
    assert result["thresholds"]["only"] == {KEY: 77}
    assert result["thresholds"]["shared"] != {KEY: 90}
    ((problem, _),) = fits
    assert set(problem.radars) == {"shared"}
    assert [zone.group_id for zone in problem.zones] == ["good"]
    assert result["status"] == "uncertain"
    assert result["after"]["room"] is None
    assert set(result["after"]["devices"]) == {"shared", "only"}
    assert_room_contract(result["groups"]["good"]["after"])


def test_no_scorable_zone_raises_original_reason(tmp_path, fits):
    values, labels = alternating(4)
    item = member(tmp_path, "r", values)
    present = [label for label in labels if label["state"] == "present"]
    empty = [label for label in labels if label["state"] == "not_present"]
    groups = {"b": {"r": item}, "a": {"r": item}}
    rooms = {"a": room("a", present), "b": room("b", empty)}
    expected = facade.measure({"r": item}, {"r": {KEY: 90}}, 0, 480, rooms["a"])["room"]
    assert expected["error_cost"] is None
    with pytest.raises(ValueError) as raised:
        facade.fit_set(groups, 0, 480, rooms)
    assert str(raised.value) == expected["reason"]
    assert not fits
    with pytest.raises(ValueError, match="No zones"):
        facade.fit_set({}, 0, 480)


def test_fixed_comparison_never_calls_fit(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("fixed-pattern scoring must never refit")

    monkeypatch.setattr(engine, "fit", forbidden)
    monkeypatch.setattr(search, "run_search", forbidden)
    values, labels = alternating(6)
    item = member(tmp_path, "r", values, labels)
    view = item["view"]
    context = {
        "keys": [KEY],
        "timing": {},
        "patterns": {
            "user": {"id": "u1", "thresholds": {KEY: 30}, "applicable": True},
            "automatic": {"id": "a1", "thresholds": {KEY: 70}, "applicable": True},
            "previous": None,
            "live": {"id": None, "thresholds": {KEY: 90}, "applicable": False},
        },
        "quality_version": 6,
    }
    compared = facade.compare(view, "r", context)
    assert compared["best_slots"] == ["user"]
    assert compared["patterns"]["previous"]["applicable"] is False
    assert compared["counts"]["human"] == {"present": 60, "not_present": 60}
    assessed = facade.assess(view, "r", {KEY: 30})
    assert assessed["error_cost"] == compared["patterns"]["user"]["error_cost"] == 0
    measured = facade.measure({"r": item}, {"r": {KEY: 30}}, 0, 720)
    assert measured["room"]["error_cost"] == 0
    assert facade.compare_room({"r": item}, {"r": {KEY: 30}}, 0, 720)["candidate"]["score"] == 100


def test_automatic_comparison_cost_equals_measurement(tmp_path):
    values, labels = alternating(6, last_present=QUIET)
    item = member(tmp_path, "r", values, labels)
    candidate = {"r": {KEY: 40}}
    compared = facade.compare_room({"r": item}, candidate, 0, 720)
    measured = facade.measure({"r": item}, candidate, 0, 720)["room"]
    live = facade.measure({"r": item}, {"r": {KEY: 90}}, 0, 720)["room"]
    assert compared["candidate"]["error_cost"] == measured["error_cost"] > 0
    assert compared["live"]["error_cost"] == live["error_cost"] > measured["error_cost"]
    assert compared["candidate"]["mostly_missed_episodes"] == measured["mostly_missed_episodes"]
    context = {
        "keys": [KEY],
        "timing": {},
        "patterns": {"user": {"id": "u", "thresholds": {KEY: 40}, "applicable": True}},
    }
    pattern = facade.compare(item["view"], "r", context)["patterns"]["user"]
    assessed = facade.assess(item["view"], "r", {KEY: 40})
    assert pattern["error_cost"] == assessed["error_cost"] == measured["error_cost"]
    assert pattern["ranking"][0] == pattern["error_cost"]


def test_device_fit_report_keeps_the_panel_contract(tmp_path):
    values, labels = alternating(10)
    values[65:70] = [60] * 5  # A 30-second empty spike as strong as presence.
    report = device_fit(worker_view(tmp_path, "r", values, labels), weight=0.5)
    assert_device_contract(report)
    assert report["validation"] is not None and report["current_validation"] is not None
    assert report["status"] == "tradeoff"
    assert report["counts"] == {"present": 100, "not_present": 100}
    assert report["training"]["empty_periods_failed"] == 1
    assert report["training"]["empty_periods_mostly_false_active"] == 0
    (influence,) = report["review"]["influence"]
    assert (influence["start"], influence["end"], influence["state"]) == (390, 420, "not_present")
    assert influence["error_seconds"] == 30
    assert influence["score_without_period"] == 100


def test_room_reports_keep_the_assessment_contract(tmp_path):
    values, labels = alternating(6)
    a = member(tmp_path, "a", values, labels)
    b = member(tmp_path, "b", [10] * len(values), labels)
    members = {"a": a, "b": b}
    measured = facade.measure(members, {"a": {KEY: 30}, "b": {KEY: 30}}, 0, 720)
    assert_room_contract(measured)
    assert measured["devices"]["a"]["exclusive_presence_seconds"] == 360
    # Both members carry the labels, so each is attributed the presence: only b misses it.
    assert measured["devices"]["b"]["missed_seen_seconds"] == 360
    assert measured["devices"]["a"]["missed_seen_seconds"] == 0
    assert measured["room"]["presence_recall"] == 1
    fitted = facade.room_fit(members, 0, 720)
    assert_room_contract(fitted["before"])
    assert_room_contract(fitted["after"])
    assert fitted["method"] == ROOM_METHOD == "joint_room_v2"


def test_seen_by_counts_missed_attributed_presence(tmp_path):
    values, labels = alternating(6)
    item = member(tmp_path, "r", values, labels)
    measured = facade.measure({"r": item}, {"r": {KEY: 70}}, 0, 720)
    radar = measured["devices"]["r"]
    assert radar["missed_seen_seconds"] == pytest.approx(360)
    assert radar["missed_seen_automatic_seconds"] == 0
    assert radar["occupied_seconds"] == 360


def anomalous_trace():
    """Presence at 80, then a 90-energy stretch labelled empty that the audit discards."""
    values = [80] * 100 + [0] * 10 + [90] * 40 + [0] * 250  # [0,600) [600,660) [660,900) ...
    labels = [
        {"start": 0, "end": 600, "state": "present"},
        {"start": 600, "end": 960, "state": "not_present"},
        {"start": 966, "end": 2400, "state": "not_present"},
    ]
    return values, labels


def test_discarded_anomaly_time_is_unobserved_in_the_report(tmp_path, fits):
    values, labels = anomalous_trace()
    report = facade.fit(worker_view(tmp_path, "r", values, labels), "r", [KEY], {KEY: 95})
    problem, result = fits[-1]
    anomaly = report["anomaly"]
    assert anomaly["status"] == "discarded"
    training = report["training"]
    # The judge sees exactly what the fitter saw: the discarded stretch is not empty time.
    assert training["duration"]["empty_seconds"] == result.after["empty_seconds"] < 1794
    assert training["duration"]["error_cost"] == result.after["cost"] == report["base_cost"]
    assert training["empty_periods_failed"] == result.after["empty_periods_failed"] == 0


def test_room_discarded_anomaly_time_is_unobserved_in_the_report(tmp_path, fits):
    values, labels = anomalous_trace()
    groups = {"zone": {"r": member(tmp_path, "r", values, threshold=95)}}
    report = facade.fit_set(groups, 0, 2400, {"zone": room("zone", labels)})
    _problem, result = fits[-1]
    assert report["anomaly"]["status"] == "discarded"
    after = report["after"]["room"]
    assert report["cost_after"] == after["error_cost"] == result.after["cost"]
    assert after["empty_periods_failed"] == result.after["empty_periods_failed"]


def test_room_report_judges_with_the_policy_its_backtest_selected(tmp_path, fits, monkeypatch):
    import test_rooms

    # Human presence at 80, an automatic present estimate at 30, human empty at 35:
    # the reference policy ignores the estimate on the human-supported class.
    values = [80] * 100 + [30] * 50 + [35] * 250
    labels = [
        {"start": 0, "end": 600, "state": "present"},
        {"start": 900, "end": 2400, "state": "not_present"},
    ]
    item = test_rooms.member("r", [], [])
    rows = [
        (i * 6, [v] + [255] * 17 + ([1, 90] if 100 <= i < 150 else [0, 0]))
        for i, v in enumerate(values)
    ]
    test_rooms.set_records(item["view"], "r", rows)
    item["thresholds"] = {KEY: 95}
    groups, rooms = {"zone": {"r": item}}, {"zone": room("zone", labels)}

    def reference(problem, *_args):
        return "reference", facade._room_audit(problem.policy.quality["models"])

    monkeypatch.setattr(facade, "_room_policy", reference)
    contexts = adaptive_contexts(groups, rooms)
    report = facade.fit_set(groups, 0, 2400, rooms, 1.0, quality_contexts=contexts)
    final, result = fits[-1]
    assert final.policy.reference is True
    assert report["groups"]["zone"]["cost_after"] == result.after["cost"]
    assert report["cost_after"] == report["after"]["room"]["error_cost"] == result.after["cost"]


def adaptive_contexts(groups, rooms):
    from tuner_under_test.rooms import adaptive

    return adaptive.collect_contexts(groups, 0, 2400, rooms)


def test_adaptive_policy_choice_uses_the_full_held_out_cost():
    from tuner_under_test.engine.hybrid import held

    # Ten 10-second occupied episodes and one 100-second empty period with 10 s false-active.
    # Reference misses 4 s of each; corrected misses 9.75 s of four: less missed time, but
    # four mostly-missed episodes and a 195-point worse common cost.
    counts = dict.fromkeys(held.COUNTS, 0) | {"occupied_episodes": 10, "empty_periods": 1}
    reference = {
        "present_seconds": 100,
        "empty_seconds": 100,
        "missed_seconds": 40,
        "false_seconds": 10,
        "missed_episodes": 0,
        "cost": 310,
        **counts,
        "empty_periods_failed": 1,
    }
    positive = {**reference, "missed_seconds": 39, "cost": 505, "mostly_missed_episodes": 4}
    reports_ = {"reference": {"score": reference}, "corrected_positive": {"score": positive}}
    choice = held.decide(reports_, [[10], [10]])
    assert (choice.policy, choice.validated) == ("reference", True)
    assert choice.reason == "held_presence_regression"


def test_device_status_uses_human_primary_classes():
    import test_tuner as harness

    human = [(0, {KEY: 14}, "present"), (6, {KEY: 10}, "not_present")]
    automatic = [(-120 + i * 6, {KEY: 10}, "present", 0.9) for i in range(20)]
    report = harness.fit(human, [KEY], current={KEY: 12}, automatic=automatic)
    assert report["training"]["sensitivity"] == 1
    assert report["status"] == "ok"


def test_room_recall_uses_the_reported_primary_source():
    import test_rooms

    labels = [
        {"start": 0, "end": 600, "state": "present"},
        {"start": 1200, "end": 1800, "state": "not_present"},
    ]
    item = test_rooms.member("r", [], [])
    records = [
        (i * 6, [80 if i < 100 else 10] + [255] * 17 + ([1, 90] if 100 <= i < 200 else [0, 0]))
        for i in range(300)
    ]
    test_rooms.set_records(item["view"], "r", records)
    report = facade.measure({"r": item}, {"r": {KEY: 20}}, 0, 1800, room("zone", labels))
    measured = report["room"]
    assert measured["sources"]["present"] == "human"
    assert measured["outcomes"]["automatic"]["present"]["seconds"] > 0
    assert measured["presence_recall"] == 1
    assert measured["target_met"] is True
    fitted = facade.room_fit({"r": item}, 0, 1800, room("zone", labels))
    assert fitted["status"] == "ok"

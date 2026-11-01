"""Private-path validation uses synthetic exports only."""

import base64
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_tuner  # noqa: F401
from hybrid_referee import referee_score
from tuner_under_test.history import dayfile

ROOT = Path(__file__).resolve().parents[1]


def cli():
    path = ROOT / "tools" / "validate_hybrid.py"
    assert path.is_file(), "Task 18 validation CLI is missing"
    spec = importlib.util.spec_from_file_location("validate_hybrid_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def synthetic(tmp_path, episodes=4):
    history = tmp_path / "input"
    history.mkdir()
    builder = dayfile.DayBuilder(0, 86400)
    labels = []
    for i in range(episodes * 2):
        start = i * 120
        present = i % 2 == 0
        builder.add(dayfile.Change(start, {0: 80 if present else 10}))
        labels.append(
            {
                "start": start,
                "end": start + 120,
                "state": "present" if present else "not_present",
                "text": "private label text",
            }
        )
    builder.add(dayfile.Change(episodes * 240, {}))
    day = builder.day()
    device = {
        "name": "private bedroom",
        "current_thresholds": {"g0_move": 50},
        "timing_configuration": {},
        "history": {
            "labels": labels,
            "day_files": {"1970-01-01": base64.b64encode(dayfile.encode_day(day)).decode("ascii")},
        },
    }
    path = history / "export.json"
    path.write_text(
        json.dumps({"format": "ld2410_tuner_export_v2", "devices": {"private-device-id": device}}),
        encoding="utf-8",
    )
    return history, path, day


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_validation_requires_user_history_path(tmp_path, monkeypatch):
    module = cli()
    with pytest.raises(SystemExit) as exc:
        module.main(["--output", str(tmp_path / "out")])
    assert exc.value.code == 2
    assert (
        module.main(["--history", str(tmp_path / "missing"), "--output", str(tmp_path / "out")])
        == 2
    )
    assert not (tmp_path / "out").exists()
    history, _, _ = synthetic(tmp_path, episodes=2)
    monkeypatch.setattr(module, "_rss", lambda: 1024**3)
    assert module.main(["--history", str(history), "--output", str(tmp_path / "failed")]) == 1


def test_validation_never_writes_inside_history(tmp_path, monkeypatch):
    module = cli()
    history, _, _ = synthetic(tmp_path)
    before = snapshot(history)
    for out in (history, history / "report", ROOT / "forbidden-validation"):
        with pytest.raises(ValueError):
            module.validate(history, out)
    assert snapshot(history) == before
    module.validate(history, tmp_path / "out")
    assert snapshot(history) == before
    with monkeypatch.context() as clock:
        clock.setattr(module.time, "time", lambda: 0)
        context = module.load_history(history, tmp_path / "live-detached")
        assert all(not entry["persistent"] for entry in context.view._days.entries.values())
        module.modules().engine.fit(
            module.training_problem(context, "private-device-id", [(0, 180)])
        )
        assert not list(context.detached.glob("*/*.cell"))
    link = tmp_path / "alias"
    try:
        link.symlink_to(history, target_is_directory=True)
    except OSError:
        return  # Windows may not grant symlink creation; direct paths were checked above.
    with pytest.raises(ValueError):
        module.validate(history, link / "out")


@pytest.mark.parametrize("episodes", [2, 3, 4])
def test_anonymous_report_matches_independent_referee(tmp_path, episodes):
    module = cli()
    history, path, day = synthetic(tmp_path, episodes)
    timing = {2: {}, 3: {"timeout": 60}, 4: {"timeout": 120, "on_delay": 1, "off_delay": 0}}[
        episodes
    ]
    data = json.loads(path.read_text())
    data["devices"]["private-device-id"]["timing_configuration"] = timing
    path.write_text(json.dumps(data))
    report = module.validate(history, tmp_path / "out")
    assert report["protocol"] == "exp2-cv-v2"
    assert report["status"] == "ok"
    assert report["sets"][0]["alias"] == "R1"
    radar = SimpleNamespace(times=day.times, ends=day.ends, columns=day.columns, timing=timing)
    for fold in report["sets"][0]["folds"]:
        a, b = fold["heldout"][0]
        truth = [
            (i * 120, (i + 1) * 120, "present" if i % 2 == 0 else "not_present", "human", 1)
            for i in range(episodes * 2)
        ]
        expected = referee_score(
            {"R1": radar},
            {"R1": {0: fold["thresholds"]["R1"]["0"]}},
            truth,
            (a, b),
            include_margins=False,
        )
        for key in (
            "cost",
            "occupied_recall",
            "mostly_missed_episodes",
            "longest_miss_seconds",
            "false_triggers",
            "empty_periods_failed",
            "false_active_seconds",
            "empty_periods_with_false_trigger",
            "empty_periods_mostly_false_active",
        ):
            assert fold["primary"][key] == pytest.approx(expected[key])
            assert fold["exact"][key] == pytest.approx(expected[key])
        assert fold["legal"]
        assert set(fold["performance"]) == {"cold", "warm"}
        assert fold["parity"] is True
        assert {"base_cost", "quiet_cost", "selection_cost", "row_work", "anomaly"} <= fold[
            "fit"
        ].keys()
        assert "primary" in fold["start"]
    raw = (tmp_path / "out" / "report.json").read_text(encoding="utf-8")
    assert json.loads(raw) == report
    assert all(
        secret not in raw
        for secret in ("private-device-id", "private bedroom", "private label text", str(history))
    )
    assert report["hashes"]["software"] and report["hashes"]["constants"]
    pooled = report["sets"][0]["aggregate"]
    assert pooled["occupied_seconds"] == episodes * 120
    assert pooled["empty_seconds"] == episodes * 120
    if episodes == 3:
        assert pooled["false_active_seconds"] == episodes * 60
        # Two fold boundaries bisect an empty period: the active halves fail, while
        # the unsplit middle period (exactly half active) passes.
        assert pooled["empty_periods_failed"] == 2
        whole = referee_score(
            {"R1": radar}, {"R1": {0: 45}}, truth, (0, episodes * 240), include_margins=False
        )
        assert whole["empty_periods_failed"] == 0
    if episodes == 4:
        assert pooled["empty_periods_mostly_false_active"] > 0
        assert pooled["empty_periods_with_false_trigger"] == 0
    assert report["sets"][0]["plan"]["kind"] == (
        "balanced4" if episodes == 4 else "leave_one_occupied_episode_out"
    )
    if episodes != 2:
        return
    baseline = tmp_path / "frozen.json"
    baseline.write_text(json.dumps(report))
    compared = module.validate(history, tmp_path / "compared", baseline=baseline)
    assert compared["baseline"]["status"] == "ok"
    assert (
        compared["sets"][0]["folds"][0]["baseline"]["primary"]
        == report["sets"][0]["folds"][0]["primary"]
    )
    report["sets"][0]["folds"][0]["thresholds"]["R1"]["0"] = 100
    baseline.write_text(json.dumps(report))
    assert (
        module.validate(history, tmp_path / "regression", baseline=baseline)["status"] == "failed"
    )


def test_primary_truth_excludes_heuristics(tmp_path):
    module = cli()
    history, path, _ = synthetic(tmp_path)
    data = json.loads(path.read_text())
    data["devices"]["private-device-id"]["history"]["labels"][1]["end"] = 180
    path.write_text(json.dumps(data))
    first = module.validate(history, tmp_path / "first")
    data = json.loads(path.read_text())
    dev = data["devices"]["private-device-id"]
    dev["heuristic_labels"] = [{"start": 0, "end": 960, "state": "not_present"}]
    path.write_text(json.dumps(data))
    second = module.validate(history, tmp_path / "second")
    assert first["sets"][0]["aggregate"] == second["sets"][0]["aggregate"]
    assert second["secondary_truth"] == {"circular": True, "used_for_verdict": False}
    assert "secondary" in second["sets"][0]["folds"][0]
    assert (
        second["sets"][0]["secondary_aggregate"]["empty_seconds"]
        > second["sets"][0]["aggregate"]["empty_seconds"]
    )
    # Storage/room input uses the same adapters and never emits a group's private identity.
    data["groups"] = {"private-room": {"device_ids": ["private-device-id"]}}
    data["room_labels"] = {
        "private-room": [{**label, "source": "manual"} for label in dev["history"]["labels"]]
    }
    path.write_text(json.dumps(data))
    grouped = module.validate(history, tmp_path / "grouped")
    assert [s["kind"] for s in grouped["sets"]] == ["device", "room"]
    assert grouped["sets"][1]["status"] == "ok"
    assert "private-room" not in json.dumps(grouped)
    encoded = dev["history"].pop("day_files")
    native = history / "history" / "private-device-id"
    native.mkdir(parents=True)
    (native / "1970-01-01.day").write_bytes(base64.b64decode(encoded["1970-01-01"]))
    path.write_text(json.dumps({"data": data}))
    storage = module.validate(history, tmp_path / "storage")
    assert storage["sets"][1]["aggregate"] == grouped["sets"][1]["aggregate"]
    # A native .storage directory uses the actual HA envelope and on-disk layout.
    store_path = history / "ld2410_tuner.data"
    path.replace(store_path)
    stored_days = history / "ld2410_tuner" / "history" / "private-device-id"
    stored_days.mkdir(parents=True)
    (native / "1970-01-01.day").replace(stored_days / "1970-01-01.day")
    actual_storage = module.validate(history, tmp_path / "actual-storage")
    assert actual_storage["sets"][1]["aggregate"] == storage["sets"][1]["aggregate"]
    records = [dayfile.Change(i * 120, {0: 80 if i % 2 == 0 else 10}) for i in range(8)]
    records.append(dayfile.Change(960, {}))  # Last observed instant, no invented tail.
    (stored_days / "1970-01-01.day").unlink()
    (stored_days / "1970-01-01.log").write_bytes(dayfile.encode_records(records, 0))
    before = snapshot(history)
    live = module.validate(history, tmp_path / "live-storage")
    assert live["sets"][1]["aggregate"] == actual_storage["sets"][1]["aggregate"]
    assert snapshot(history) == before


def test_holdout_labels_never_enter_training(tmp_path):
    module = cli()
    history, path, _ = synthetic(tmp_path, episodes=5)
    data = json.loads(path.read_text())
    dev = data["devices"]["private-device-id"]
    dev["threshold_constraints"] = {"g0_move": {"min": 0, "max": 98, "step": 7}}
    dev["learning_results"] = {
        "saved": {
            "id": "private-result",
            "applied_at": -1,
            "configuration": {"g0_move": 50},
            "proposals": {"g0_move": {"threshold": 10.5}},
        }
    }
    dev["last_applied"] = {"g0_move": 10.5}
    path.write_text(json.dumps(data))
    report = module.validate(history, tmp_path / "out")
    item = report["sets"][0]
    assert item["plan"]["occupied_episodes_per_block"] == [2, 1, 1, 1]
    for fold in item["folds"]:
        for a, b in fold["training_regions"]:
            assert all(min(b, y) <= max(a, x) for x, y in fold["heldout"])
        assert fold["training_counts"]["occupied_seconds"] > 0
        assert fold["held_counts"]["occupied_seconds"] > 0
        assert fold["start_provenance"] == {"R1": "recovered"}
        assert fold["start"]["thresholds"]["R1"]["0"] == 14  # Legal-domain ties upward.
        assert fold["thresholds"]["R1"]["0"] % 7 == 0
    # Observable independence: radically change one held-out episode's truth, retain the mask.
    context = module.load_history(history, tmp_path / "detached")
    problem = module.training_problem(context, "private-device-id", [(0, 360)])
    dev = context.devices["private-device-id"]
    dev["history_labels"][0]["state"] = "not_present"
    changed = module.training_problem(context, "private-device-id", [(0, 360)])
    assert problem.zones == changed.zones
    assert all(s.end <= 0 or s.start >= 360 for s in changed.zones[0].truth)
    assert (
        module.modules().engine.fit(problem).thresholds
        == module.modules().engine.fit(changed).thresholds
    )


def test_validation_records_accepted_r1_exception_without_hiding_metrics(tmp_path):
    module = cli()
    history, _, _ = synthetic(tmp_path, episodes=1)
    report = module.validate(history, tmp_path / "out")
    assert report["sets"][0]["plan"]["optimistic"]
    note = report["accepted_exception"]
    assert note["set"] == "R1" and note["fold"] == 3
    assert "drift" in note["reason"] and note["excluded_from_aggregate"] is False
    assert "longest_miss_seconds" in report["sets"][0]["aggregate"]
    assert report["input"] == "user supplied"
    # Accepted historical weakness is an annotation, never a waiver for parity failures.
    assert report["accepted_exception"]["waives_validation_failure"] is False

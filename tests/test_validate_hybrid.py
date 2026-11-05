"""Private-path validation uses synthetic exports only."""

import base64
import importlib.util
import json
import subprocess
import sys
import tempfile
from functools import partial
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
    # Synthetic contract tests must not inherit the pytest process's RSS peak.
    # Resource-failure tests override this boundary explicitly below.
    module._rss = lambda: 64 * 1024**2
    module.validate = partial(module.validate, jobs=1)
    return module


def test_synthetic_validator_uses_stable_rss_fixture():
    # The real RSS API reports this pytest process's lifetime peak, including
    # unrelated capacity tests that ran before this module.
    assert cli()._rss() == 64 * 1024**2


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
    assert (
        module.main(
            ["--history", str(history), "--output", str(tmp_path / "failed"), "--jobs", "1"]
        )
        == 1
    )


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


def test_validation_reads_history_kept_in_a_private_test_data_folder(tmp_path):
    module = cli()
    root = tmp_path / "checkout" / "tests" / "data"
    root.mkdir(parents=True)
    history, _, _ = synthetic(root)
    before = snapshot(history)
    module.validate(history, tmp_path / "out")
    assert snapshot(history) == before
    with pytest.raises(ValueError):
        module.validate(history, tmp_path / "other" / "tests" / "data" / "out")


def test_validation_starts_unsaved_radars_from_every_channel_at_50(tmp_path):
    module = cli()
    history, path, _ = synthetic(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["devices"]["private-device-id"]["current_thresholds"]
    path.write_text(json.dumps(data), encoding="utf-8")
    report = module.validate(history, tmp_path / "out")
    for fold in report["sets"][0]["folds"]:
        assert fold["start_provenance"] == {"R1": "default50"}
        assert len(fold["start"]["thresholds"]["R1"]) == 18
        assert set(fold["start"]["thresholds"]["R1"].values()) == {50}


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


@pytest.mark.parametrize("source", ["automatic", "none", "present_only"])
def test_unscorable_primary_sets_are_skipped(tmp_path, source):
    module = cli()
    history, path, _ = synthetic(tmp_path)
    data = json.loads(path.read_text())
    dev = data["devices"]["private-device-id"]
    labels = dev["history"]["labels"]
    if source == "none":
        labels.clear()
    elif source == "present_only":
        labels[:] = [s for s in labels if s["state"] == "present"]
    else:
        for label in labels:
            label["source"] = source
    path.write_text(json.dumps(data))
    report = module.validate(history, tmp_path / "out")
    item = report["sets"][0]
    assert item["status"] == "skipped"
    assert item["folds"] == []
    assert item["failures"] == []
    assert item["skip_reason"] == "missing_primary_class"
    assert report["status"] == "ok"


def test_saved_storage_configuration_and_reconstructed_room(tmp_path):
    module = cli()
    history, path, _ = synthetic(tmp_path)
    data = json.loads(path.read_text())
    dev = data["devices"]["private-device-id"]
    del dev["current_thresholds"]
    dev["learning_results"] = {
        "current": {
            "configuration": {"g0_move": 50},
            "timing_configuration": {"timeout": 60},
            "threshold_constraints": {"g0_move": {"min": 0, "max": 100, "step": 5}},
        }
    }
    dev["learning_job"] = {"id": "private-job"}
    data["devices"]["second-device"] = json.loads(json.dumps(dev))
    data["room_learning"] = {"area:private-room": {"job": {"id": "private-job"}}}
    path.write_text(json.dumps(data))
    report = module.validate(history, tmp_path / "out")
    assert [s["alias"] for s in report["sets"]] == ["R1", "R2", "G1"]
    assert all(s["status"] == "ok" for s in report["sets"])
    assert report["sets"][0]["aggregate"]["false_active_seconds"] == 240
    for fold in report["sets"][0]["folds"]:
        assert set(fold["thresholds"]["R1"]) == {"0"}
        assert fold["thresholds"]["R1"]["0"] % 5 == 0


def test_exact_parity_merges_adjacent_truth_fragments(tmp_path):
    module = cli()
    history, _, _ = synthetic(tmp_path)
    context = module.load_history(history, tmp_path / "detached")
    full = module.training_problem(context, "private-device-id", [])
    radars = module._oracle_radars(full, tmp_path / "oracle")
    truth = [
        (0, 60, "present", "human", 1),
        (60, 120, "present", "human", 1),
        (120, 240, "not_present", "human", 1),
    ]
    pattern = {"private-device-id": {0: 50}}
    expected = module._oracle_score(radars, pattern, truth, [(0, 240)])
    actual = module._score(full, pattern, truth, [(0, 240)], "exact")
    assert actual == expected


def test_verdict_explains_resource_failure(tmp_path, monkeypatch):
    module = cli()
    history, _, _ = synthetic(tmp_path, episodes=2)
    monkeypatch.setattr(module, "_rss", lambda: 1024**3)
    item = module.validate(history, tmp_path / "out")["sets"][0]
    assert item["status"] == "failed"
    assert item["failures"] == ["rss_budget"]
    assert all(f["failures"] == ["rss_budget"] for f in item["folds"])


def test_training_inputs_built_once_per_fold(tmp_path, monkeypatch):
    module = cli()
    history, _, _ = synthetic(tmp_path, episodes=2)
    original = module.training_problem
    calls = []

    def counted(*args):
        calls.append(args[2])
        return original(*args)

    monkeypatch.setattr(module, "training_problem", counted)
    report = module.validate(history, tmp_path / "out")
    assert len(calls) == 1 + len(report["sets"][0]["folds"])


def test_oracle_replays_once_for_multiple_intervals(tmp_path, monkeypatch):
    module = cli()
    history, _, _ = synthetic(tmp_path)
    context = module.load_history(history, tmp_path / "detached")
    full = module.training_problem(context, "private-device-id", [])
    radars = module._oracle_radars(full, tmp_path / "oracle")
    oracle = module.referee()
    original = oracle.replay
    calls = []

    def counted(*args):
        calls.append(args[2])
        return original(*args)

    monkeypatch.setattr(oracle, "replay", counted)
    module._oracle_score(
        radars, {"private-device-id": {0: 50}}, module._truth(full), [(0, 60), (120, 180)]
    )
    assert len(calls) == 1


def test_room_manual_truth_maps_to_radar_and_own_unknown_wins(tmp_path):
    module = cli()
    history, path, _ = synthetic(tmp_path)
    data = json.loads(path.read_text())
    dev = data["devices"]["private-device-id"]
    labels = dev["history"]["labels"]
    data["room_groups"] = {"private-room": {"device_ids": ["private-device-id"]}}
    data["room_labels"] = {"private-room": [{**s, "source": "manual"} for s in labels]}
    dev["history"]["labels"] = [{"start": 0, "end": 30, "state": "unknown"}]
    path.write_text(json.dumps(data))
    report = module.validate(history, tmp_path / "out")
    assert [s["alias"] for s in report["sets"]] == ["R1", "G1"]
    assert report["sets"][0]["aggregate"]["occupied_seconds"] == 450
    assert report["sets"][0]["aggregate"]["empty_seconds"] == 480
    assert report["sets"][1]["aggregate"]["occupied_seconds"] == 480


def test_primary_cv_keeps_all_human_history_without_production_horizon(tmp_path):
    module = cli()
    history, _, _ = synthetic(tmp_path)
    context = module.load_history(history, tmp_path / "detached")
    context.end = 100 * 86400
    problem = module.training_problem(context, "private-device-id", [])
    prepared = module.modules().engine.with_horizon(problem)
    assert prepared.zones == problem.zones


def test_exact_parity_keeps_timing_prefix_before_sparse_labels(tmp_path):
    module = cli()
    history, path, _ = synthetic(tmp_path)
    data = json.loads(path.read_text())
    dev = data["devices"]["private-device-id"]
    dev["timing_configuration"] = {"timeout": 8000}
    path.write_text(json.dumps(data))
    context = module.load_history(history, tmp_path / "detached")
    full = module.training_problem(context, "private-device-id", [])
    radars = module._oracle_radars(full, tmp_path / "oracle")
    truth = [(4000, 4100, "present", "human", 1), (4100, 4200, "not_present", "human", 1)]
    pattern = {"private-device-id": {0: 50}}
    expected = module._oracle_score(radars, pattern, truth, [(4000, 4200)])
    actual = module._score(full, pattern, truth, [(4000, 4200)], "exact")
    assert actual["false_active_seconds"] == expected["false_active_seconds"] == 100


def test_aliases_follow_saved_device_order(tmp_path):
    module = cli()
    history, path, _ = synthetic(tmp_path, episodes=2)
    data = json.loads(path.read_text())
    dev = data["devices"]["private-device-id"]
    unlabelled = json.loads(json.dumps(dev))
    unlabelled["history"]["labels"] = []
    data["devices"] = {"z-first": unlabelled, "a-second": dev}
    path.write_text(json.dumps(data))
    report = module.validate(history, tmp_path / "out")
    assert [(s["alias"], s["status"]) for s in report["sets"]] == [("R1", "skipped"), ("R2", "ok")]


def test_unlabelled_set_never_materializes_oracle_rows(tmp_path, monkeypatch):
    module = cli()
    history, path, _ = synthetic(tmp_path)
    data = json.loads(path.read_text())
    data["devices"]["private-device-id"]["history"]["labels"] = []
    path.write_text(json.dumps(data))
    original = module._oracle_radars
    calls = []

    def counted(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(module, "_oracle_radars", counted)
    report = module.validate(history, tmp_path / "out")
    assert report["sets"][0]["status"] == "skipped"
    assert not calls


def test_finished_day_wins_over_stale_log(tmp_path):
    module = cli()
    history, path, day = synthetic(tmp_path)
    data = json.loads(path.read_text())
    data["devices"]["private-device-id"]["history"].pop("day_files")
    path.write_text(json.dumps(data))
    folder = history / "history" / "private-device-id"
    folder.mkdir(parents=True)
    encoded = dayfile.encode_day(day)
    (folder / "1970-01-01.day").write_bytes(encoded)
    (folder / "1970-01-01.log").write_bytes(
        dayfile.encode_records([dayfile.Change(0, {0: 99}), dayfile.Change(60, {})], 0)
    )
    detached = tmp_path / "detached"
    module.load_history(history, detached)
    assert (detached / "private-device-id" / "1970-01-01.day").read_bytes() == encoded


def test_incomplete_training_fold_is_scored_without_failing_set(tmp_path):
    module = cli()
    history, path, _ = synthetic(tmp_path, episodes=2)
    data = json.loads(path.read_text())
    labels = data["devices"]["private-device-id"]["history"]["labels"]
    labels.pop()  # Both classes in the set; first fold owns the only empty period.
    labels[1]["end"] = 150
    path.write_text(json.dumps(data))
    item = module.validate(history, tmp_path / "out")["sets"][0]
    assert any(not fold["sufficient_training"] for fold in item["folds"])
    assert item["aggregate"]["cost"] is not None
    assert item["status"] == "ok"
    assert item["failures"] == []


def test_exact_scores_share_record_scan_between_heldout_intervals(tmp_path, monkeypatch):
    module = cli()
    history, _, _ = synthetic(tmp_path)
    context = module.load_history(history, tmp_path / "detached")
    full = module.training_problem(context, "private-device-id", [])
    replay_module = module.modules().exact.replay.__globals__
    records = replay_module["Records"]
    original = records.read
    calls = []

    def counted(self):
        calls.append(1)
        return original(self)

    monkeypatch.setattr(records, "read", counted)
    module._score(
        full, {"private-device-id": {0: 50}}, module._truth(full), [(0, 60), (120, 180)], "exact"
    )
    assert len(calls) == 1


def test_missing_selected_channel_coverage_skips_human_labels(tmp_path):
    module = cli()
    history, path, _ = synthetic(tmp_path)
    data = json.loads(path.read_text())
    data["devices"]["private-device-id"].pop("current_thresholds")
    path.write_text(json.dumps(data))
    context = module.load_history(history, tmp_path / "detached")
    problem = module.training_problem(context, "private-device-id", [])
    assert len(problem.radars["private-device-id"].current) == 18
    assert set(problem.radars["private-device-id"].current.values()) == {50}
    item = module.validate(history, tmp_path / "out")["sets"][0]
    assert item["status"] == "skipped"
    assert item["failures"] == []
    assert item["folds"] == []


@pytest.mark.parametrize(
    "reason,flags,metric",
    [
        ("illegal_thresholds", (False, True, True), None),
        ("nondeterministic", (True, False, True), None),
        ("exact_parity", (True, True, False), None),
        ("cpu_budget", (True, True, True), "cpu_seconds"),
        ("traced_memory_budget", (True, True, True), "traced_peak_bytes"),
        ("rss_budget", (True, True, True), "rss_peak_bytes"),
    ],
)
def test_failure_codes_distinguish_validation_gates(reason, flags, metric):
    module = cli()
    perf = {"cpu_seconds": 0, "traced_peak_bytes": 0, "rss_peak_bytes": 0}
    if metric:
        perf[metric] = float("inf")
    assert module._failures(*flags, {"cold": perf, "warm": perf}) == [reason]


def test_room_without_recorded_members_is_skipped(tmp_path):
    module = cli()
    history, path, _ = synthetic(tmp_path, episodes=2)
    data = json.loads(path.read_text())
    labels = data["devices"]["private-device-id"]["history"]["labels"]
    data["groups"] = {"empty-room": {"device_ids": []}}
    data["room_labels"] = {"empty-room": [{**s, "source": "manual"} for s in labels]}
    path.write_text(json.dumps(data))
    report = module.validate(history, tmp_path / "out")
    room = report["sets"][1]
    assert room["status"] == "skipped"
    assert room["folds"] == []
    assert room["failures"] == []


def multi_history(tmp_path):
    history, path, _ = synthetic(tmp_path, episodes=2)
    data = json.loads(path.read_text())
    device = data["devices"]["private-device-id"]
    skipped = json.loads(json.dumps(device))
    skipped["history"]["labels"] = []
    data["devices"] = {"slow-first": device, "skipped-second": skipped, "third": device}
    data["groups"] = {"room": {"device_ids": ["slow-first", "third"]}}
    data["room_labels"] = {
        "room": [{**label, "source": "manual"} for label in device["history"]["labels"]]
    }
    path.write_text(json.dumps(data))
    return history


def without_performance(value):
    if isinstance(value, dict):
        return {k: without_performance(v) for k, v in value.items() if k != "performance"}
    if isinstance(value, list):
        return [without_performance(v) for v in value]
    return value


def test_parallel_matches_serial_and_cleans_worker_copies(tmp_path, monkeypatch):
    module = cli()
    history = multi_history(tmp_path)
    before = snapshot(history)
    serial = module.validate(history, tmp_path / "serial", jobs=1)
    baseline = tmp_path / "serial" / "report.json"
    serial = module.validate(history, tmp_path / "frozen-serial", baseline=baseline, jobs=1)
    temporary_root = tmp_path / "temporary"
    temporary_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temporary_root))
    parallel = module.validate(history, tmp_path / "parallel", baseline=baseline, jobs=2)
    assert without_performance(serial) == without_performance(parallel)
    assert [(s["alias"], s["status"]) for s in parallel["sets"]] == [
        ("R1", "ok"),
        ("R2", "skipped"),
        ("R3", "ok"),
        ("G1", "ok"),
    ]
    assert snapshot(history) == before
    assert list(temporary_root.iterdir()) == []


@pytest.mark.parametrize("jobs", ["0", "-1", "no", "1.5"])
def test_jobs_cli_rejects_invalid_values(tmp_path, jobs):
    module = cli()
    with pytest.raises(SystemExit) as exc:
        module.main(["--history", str(tmp_path), "--output", str(tmp_path / "out"), "--jobs", jobs])
    assert exc.value.code == 2
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("jobs", [0, -1, 1.5, "2", True])
def test_validate_rejects_invalid_jobs(tmp_path, jobs):
    with pytest.raises(ValueError, match="jobs"):
        cli().validate(tmp_path, tmp_path / "out", jobs=jobs)


def test_default_jobs_uses_physical_core_estimate(monkeypatch):
    module = cli()
    for cores, expected in [(None, 1), (1, 1), (2, 1), (16, 8), (64, 8)]:
        monkeypatch.setattr(module.os, "cpu_count", lambda cores=cores: cores)
        assert module._default_jobs() == expected


@pytest.mark.parametrize("failure", ["exception", "death", "initializer"])
def test_real_worker_failure_exits_without_report(tmp_path, failure):
    history = multi_history(tmp_path)
    harness = tmp_path / "run_failure.py"
    harness.write_text(
        "import sys, multiprocessing, threading, time, tempfile\n"
        f"sys.path.insert(0, {str(ROOT / 'tools')!r})\n"
        "import validate_hybrid as validator\n"
        "def fail_task(rid, alias):\n"
        "    validator._worker_set(rid, alias)\n"
        "    raise RuntimeError('Injected unexpected worker failure')\n"
        "def kill_worker():\n"
        "    while not multiprocessing.active_children():\n"
        "        time.sleep(0.001)\n"
        "    multiprocessing.active_children()[0].terminate()\n"
        "if __name__ == '__main__':\n"
        f"    tempfile.tempdir = {str(tmp_path / 'temporary')!r}\n"
        + (
            "    validator._worker_set = fail_task\n"
            if failure == "exception"
            else (
                "    original = validator._worker_snapshot\n"
                "    def broken(context):\n"
                "        result = original(context)\n"
                f"        result['entries'][0]['path'] = {str(tmp_path / 'private-identity.day')!r}\n"
                "        return result\n"
                "    validator._worker_snapshot = broken\n"
                if failure == "initializer"
                else "    threading.Thread(target=kill_worker, daemon=True).start()\n"
            )
        )
        + f"    raise SystemExit(validator.main(['--history', {str(history)!r}, "
        f"'--output', {str(tmp_path / 'out')!r}, '--jobs', '2']))\n"
    )
    (tmp_path / "temporary").mkdir()
    before = snapshot(history)
    result = subprocess.run([sys.executable, str(harness)], capture_output=True, timeout=60)
    assert result.returncode == 1, result.stderr.decode()
    assert b"private-identity" not in result.stderr
    assert not (tmp_path / "out" / "report.json").exists()
    assert list((tmp_path / "temporary").iterdir()) == []
    assert snapshot(history) == before


def test_parallel_order_is_independent_of_completion(tmp_path, monkeypatch):
    module = cli()
    history = multi_history(tmp_path)
    helper = tmp_path / "ordered_worker.py"
    marker = tmp_path / "second-finished"
    helper.write_text(
        "import os, time\n"
        "from pathlib import Path\n"
        "import validate_hybrid as validator\n"
        "def run(rid, alias):\n"
        f"    marker = Path({str(marker)!r})\n"
        "    if alias == 'R1':\n"
        "        until = time.monotonic() + 30\n"
        "        while not marker.exists():\n"
        "            if time.monotonic() > until:\n"
        "                raise RuntimeError('second set did not finish')\n"
        "            time.sleep(0.01)\n"
        "    result = validator._worker_set(rid, alias)\n"
        f"    Path({str(tmp_path)!r}, alias + '.directory').write_text(\n"
        "        str(validator._WORKER_CONTEXT.detached))\n"
        "    if alias == 'R2':\n"
        "        marker.touch()\n"
        "    return result\n"
    )
    monkeypatch.syspath_prepend(str(ROOT / "tools"))
    monkeypatch.syspath_prepend(str(tmp_path))
    worker = __import__("validate_hybrid")
    ordered = __import__("ordered_worker")
    monkeypatch.setattr(worker, "_worker_set", ordered.run)
    original = module.as_completed
    completed = []

    def record(futures):
        for future in original(futures):
            completed.append(future.result()["alias"])
            yield future

    monkeypatch.setattr(module, "as_completed", record)
    report = module.validate(history, tmp_path / "out", jobs=2)
    assert completed.index("R2") < completed.index("R1")
    assert [item["alias"] for item in report["sets"]] == ["R1", "R2", "R3", "G1"]
    directories = [Path(p.read_text()) for p in tmp_path.glob("*.directory")]
    assert len(set(directories)) == 2
    assert all(not path.exists() for path in directories)


def test_script_cli_runs_spawn_workers(tmp_path):
    history = multi_history(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "validate_hybrid.py"),
            "--history",
            str(history),
            "--output",
            str(tmp_path / "out"),
            "--jobs",
            "2",
        ],
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert json.loads(result.stdout)["status"] == "ok"
    report = json.loads((tmp_path / "out" / "report.json").read_text())
    assert report["sets"][0]["folds"][0]["performance"]["cold"]["rss_scope"] == (
        "worker process lifetime high-water mark"
    )


@pytest.mark.parametrize("jobs", [1, 2])
@pytest.mark.parametrize("sets", [None, {}, [{"alias": "R1"}], [{"alias": "R1", "folds": [{}]}]])
def test_malformed_baseline_is_invalid_before_workers(tmp_path, jobs, sets):
    module = cli()
    history, _, _ = synthetic(tmp_path, episodes=2)
    baseline = {
        "protocol": module.PROTOCOL,
        "tolerances": module.TOLERANCES,
        "hashes": {"constants": module.hashes()["constants"]},
    }
    if sets is not None:
        baseline["sets"] = sets
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(baseline))
    assert (
        module.main(
            [
                "--history",
                str(history),
                "--output",
                str(tmp_path / "out"),
                "--baseline",
                str(path),
                "--jobs",
                str(jobs),
            ]
        )
        == 2
    )
    assert not (tmp_path / "out" / "report.json").exists()


@pytest.mark.parametrize("jobs", [1, 2])
@pytest.mark.parametrize("invalid", ["timing", "room_member"])
def test_invalid_worker_input_keeps_exit_code_two(tmp_path, jobs, invalid):
    module = cli()
    history, path, _ = synthetic(tmp_path, episodes=2)
    data = json.loads(path.read_text())
    if invalid == "timing":
        data["devices"]["private-device-id"]["timing_configuration"] = {"timeout": "invalid"}
    else:
        data["groups"] = {"room": {"device_ids": ["missing-device"]}}
    path.write_text(json.dumps(data))
    assert (
        module.main(
            [
                "--history",
                str(history),
                "--output",
                str(tmp_path / "out"),
                "--jobs",
                str(jobs),
            ]
        )
        == 2
    )
    assert not (tmp_path / "out" / "report.json").exists()

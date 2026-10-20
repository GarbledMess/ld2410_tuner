"""Whole-job learning uses detached columns and the existing numerical fitter."""

from concurrent.futures import ThreadPoolExecutor

import pytest
import test_tuner  # noqa: F401
from test_event_fitting import view
from test_event_quality import block
from tuner_under_test.worker import calculations, capture, codec


def test_detached_learning_matches_existing_numerical_path(tmp_path):
    from tuner_under_test.calibration.event_fitting import fit

    source = view(
        [block(0, 600, 40), block(600, 1200, 0)],
        [
            {"start": 0, "end": 600, "state": "present"},
            {"start": 600, "end": 1200, "state": "not_present"},
        ],
    )
    entities = {"g0_move": "number.radar_g0_move_threshold"}
    current = {"g0_move": 20}
    timing = {"timeout": 30, "on_delay": 0, "off_delay": 0}
    constraints = {"g0_move": {"min": 20, "max": 20, "step": 1}}
    expected = fit(source, "any-name", list(entities), current, timing, constraints=constraints)
    with ThreadPoolExecutor() as executor:
        request = executor.submit(capture.prepare, source, "any-name", tmp_path).result()
    request.update(
        operation="learn",
        entities=entities,
        current=current,
        timing=timing,
        constraints=constraints,
    )
    actual = codec.unpack_result(codec.pack_result(calculations.execute(request)))
    for key, value in expected.items():
        assert actual[key] == value
    assert actual["configuration"] == current
    assert actual["entities"] == entities
    assert actual["threshold_constraints"] == constraints


def test_unknown_operation_fails_before_history_access():
    with pytest.raises(ValueError, match="operation"):
        calculations.execute({"operation": "import"})


def test_multi_view_uses_the_same_captured_file(tmp_path):
    source = view([block(0, 600, 40)], [])
    with ThreadPoolExecutor() as executor:
        request = executor.submit(
            capture.prepare_multi, {"a": source, "b": source}, tmp_path
        ).result()
    assert request["views"]["a"]["days"] == request["views"]["b"]["days"]
    assert len(list(tmp_path.rglob("*.day"))) == 1


def test_multi_view_learning_uses_encoded_parameters(tmp_path):
    source = view([block(0, 600, 40)], [])
    with ThreadPoolExecutor() as executor:
        request = executor.submit(capture.prepare_multi, {"any-name": source}, tmp_path).result()
    request.update(
        operation="learn",
        parameters=codec.encode(
            {
                "device_id": "any-name",
                "entities": {"g0_move": "number.gate"},
                "current": {"g0_move": 20},
                "constraints": {"g0_move": {"min": 20, "max": 20, "step": 1}},
            }
        ),
    )
    assert calculations.execute(request)["entities"] == {"g0_move": "number.gate"}


def test_entrypoint_refuses_unprotected_process_before_reading_request(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    script = Path(calculations.__file__).with_name("entrypoint.py")
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            str(script),
            str(tmp_path / "missing.json"),
            str(os.getpid()),
        ],
        capture_output=True,
        check=False,
    )
    if sys.platform != "linux":
        assert completed.returncode != 0
        assert b"isolated Linux" in completed.stderr
        assert b"FileNotFoundError" not in completed.stderr


def test_views_share_decoded_days_and_budget(tmp_path):
    source = view([block(0, 600, 40)], [])
    with ThreadPoolExecutor() as executor:
        request = executor.submit(
            capture.prepare_multi, {"a": source, "b": source}, tmp_path
        ).result()
    shared = calculations.JobHistory()
    first = calculations.restore(request["views"]["a"], shared)
    second = calculations.restore(request["views"]["b"], shared)
    date = first._days.days("any-name")[0]
    assert first._days.day("any-name", date) is second._days.day("any-name", date)
    assert shared.rows == 1


def test_child_entrypoint_bootstraps_only_pure_package(tmp_path, monkeypatch):
    import importlib
    import io
    import sys
    from types import SimpleNamespace

    from tuner_under_test.worker import entrypoint, protocol

    source = view([block(0, 600, 40)], [])
    with ThreadPoolExecutor() as executor:
        request = executor.submit(capture.prepare_multi, {"any-name": source}, tmp_path).result()
    request.update(
        operation="learn",
        parameters={
            "device_id": "any-name",
            "entities": {"g0_move": "number.gate"},
            "current": {"g0_move": 20},
            "constraints": {"g0_move": {"min": 20, "max": 20, "step": 1}},
        },
    )
    path = tmp_path / "request.json"
    path.write_bytes(protocol.pack("learn", request))
    entrypoint._namespace()
    limits = importlib.import_module("ld2410_child.worker.limits")
    installed = []
    monkeypatch.setattr(
        limits, "install", lambda budget, **kwargs: installed.append((budget, kwargs))
    )
    output = io.BytesIO()
    monkeypatch.setattr(sys, "argv", ["entrypoint.py", str(path), "123"])
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(buffer=output))
    entrypoint.main()
    result = codec.unpack_result(output.getvalue())
    assert result["entities"] == {"g0_move": "number.gate"}
    assert installed[0][1] == {"expected_parent": 123}
    assert "__file__" not in vars(sys.modules["ld2410_child"])
    monkeypatch.setattr(sys, "argv", ["entrypoint.py"])
    with pytest.raises(ValueError, match="Expected request"):
        entrypoint.main()


@pytest.mark.parametrize(
    "operation",
    ["comparison", "room_assess", "room_fit", "room_compare", "seen_by", "references", "recount"],
)
def test_real_numerical_operations_in_independent_python(tmp_path, operation):
    import json
    import subprocess
    import sys
    from pathlib import Path

    from tuner_under_test.worker import protocol

    source = view(
        [block(0, 600, 40), block(600, 1200, 0)],
        [
            {"start": 0, "end": 600, "state": "present"},
            {"start": 600, "end": 1200, "state": "not_present"},
        ],
    )
    with ThreadPoolExecutor() as executor:
        request = executor.submit(capture.prepare_multi, {"any-name": source}, tmp_path).result()
    member = {
        "view_key": "any-name",
        "thresholds": {"g0_move": 20},
        "timing": {"mode": "disabled"},
        "constraints": {"g0_move": {"min": 20, "max": 20, "step": 1}},
    }
    members = {"any-name": member}
    params = {
        "comparison": {
            "device_id": "any-name",
            "context": {
                "keys": ["g0_move"],
                "timing": {"mode": "disabled"},
                "patterns": {"live": {"thresholds": {"g0_move": 20}, "applicable": False}},
            },
        },
        "room_assess": {"members": members, "start": 0, "end": 1200},
        "room_fit": {
            "members": {"zone": members},
            "start": 0,
            "end": 1200,
            "rooms": {"zone": {"group_id": "zone", "groups": {}, "room_labels": {}}},
            "weight": 0,
        },
        "room_compare": {
            "members": {"zone": members},
            "start": 0,
            "end": 1200,
            "rooms": {"zone": None},
            "thresholds": {"any-name": {"g0_move": 20}},
        },
        "seen_by": {"definitions": {}, "group_id": "zone"},
        "references": {"jobs": [{"device_id": "any-name", "view_key": "any-name"}]},
        "recount": {
            "device_id": "any-name",
            "cutoff": 0,
            "index": ([0, 600, 1200], ["present", "not_present", None]),
            "dates": ["1970-01-01"],
            "live_date": "1970-01-01",
        },
    }[operation]
    request.update(operation=operation, parameters=codec.encode(params))
    direct = calculations.execute(request)
    path = tmp_path / "request.json"
    path.write_bytes(protocol.pack("learn", request))
    root = str(Path(calculations.__file__).parent.parent)
    script = "\n".join(
        [
            "import sys, types, pathlib",
            f"p=types.ModuleType('isolated_tuner');p.__path__=[{json.dumps(root)}]",
            "sys.modules[p.__name__]=p",
            "from isolated_tuner.worker import calculations, protocol, codec",
            "r=protocol.unpack(pathlib.Path(sys.argv[1]).read_bytes(),'learn')",
            "sys.stdout.buffer.write(codec.pack_result(calculations.execute(r)))",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", script, str(path)],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    decoded = codec.unpack_result(result.stdout)
    assert isinstance(decoded, (dict, list))
    if operation != "references":
        assert decoded == direct
    if operation == "comparison":
        assert decoded["patterns"]["live"]["score"] == 100
    if operation == "room_assess":
        assert decoded["room"]["score"] == 100
    if operation == "room_fit":
        assert decoded["thresholds"] == {"any-name": {"g0_move": 20}}
    if operation == "room_compare":
        assert decoded["zone"]["live"] == decoded["zone"]["candidate"]


@pytest.mark.parametrize("mutable", [False, True])
def test_summary_cache_only_persists_for_original_finished_files(tmp_path, mutable):
    from types import SimpleNamespace

    from tuner_under_test.history import dayfile, daystore, summary_cache

    store = daystore.DayStore(tmp_path / "history")
    date = daystore.date_of(100)
    if mutable:
        store.append("a", dayfile.Change(100, {0: 40}))
        store.append("a", dayfile.End(106))
    else:
        builder = dayfile.DayBuilder(*daystore.bounds(date))
        builder.add(dayfile.Change(100, {0: 40}))
        builder.add(dayfile.End(106))
        store.replace("a", date, builder.day())
    source = SimpleNamespace(data={"devices": {"a": {}}}, _days=store, _history_until=110)

    def acquire():
        request = capture.prepare(source, "a", tmp_path / "private")
        restored = calculations.restore(request)
        entry = request["days"][0]
        assert entry["persistent"] is (not mutable)
        summary = restored._days.summary("a", date)
        assert summary is not None
        sidecar = Path(entry["path"]).with_suffix(".sum")
        assert sidecar.exists() is (not mutable)
        assert store._path("a", date, ".sum").exists() is (not mutable)
        if not mutable:
            assert summary_cache.load(summary.day, store._path("a", date, ".day")) is not None

    from pathlib import Path

    with ThreadPoolExecutor() as executor:
        executor.submit(acquire).result()


def test_summary_cache_rejects_replaced_physical_source(tmp_path):
    from types import SimpleNamespace

    from tuner_under_test.history import dayfile, daystore
    from tuner_under_test.worker.history import HistoryRejected

    store = daystore.DayStore(tmp_path / "history")
    date = daystore.date_of(100)
    builder = dayfile.DayBuilder(*daystore.bounds(date))
    builder.add(dayfile.Change(100, {0: 40}))
    builder.add(dayfile.End(106))
    store.replace("a", date, builder.day())
    source = SimpleNamespace(data={"devices": {"a": {}}}, _days=store)

    def acquire():
        request = capture.prepare(source, "a", tmp_path / "private")
        restored = calculations.restore(request)
        restored._days.day("a", date)
        store.replace("a", date, builder.day())
        with pytest.raises(HistoryRejected, match="changed before summary"):
            restored._days.summary("a", date)
        assert not store._path("a", date, ".sum").exists()

    with ThreadPoolExecutor() as executor:
        executor.submit(acquire).result()

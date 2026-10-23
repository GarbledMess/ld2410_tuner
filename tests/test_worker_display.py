"""Pure display jobs retain chart geometry and lossless bounded export days."""

import base64
from concurrent.futures import ThreadPoolExecutor

import pytest
import test_tuner  # noqa: F401
from test_event_fitting import view
from test_event_quality import block
from tuner_under_test.history import dayfile
from tuner_under_test.worker import capture, codec, display


def test_display_chart_detached_matches_native_chart(tmp_path):
    from tuner_under_test.presentation.charts import history_series_multi
    from tuner_under_test.worker import calculations

    source = view([block(0, 600, 40)], [])
    parameters = {
        "device_id": "any-name",
        "keys": ["g0_move"],
        "hours": 0.5,
        "max_points": 100,
        "end": 1200,
    }
    expected = history_series_multi(source, **parameters)
    with ThreadPoolExecutor() as executor:
        request = executor.submit(capture.prepare_multi, {"any-name": source}, tmp_path).result()
    request.update(operation="charts", parameters=codec.encode(parameters))
    actual = display.unpack_chart(calculations.execute(request))
    assert actual == expected


@pytest.mark.parametrize("gate_count,max_points", [(9, 400), (18, 1000)])
def test_full_day_chart_crosses_real_manifest_and_result_transport(
    tmp_path, gate_count, max_points
):
    from test_event_charts import row
    from test_event_charts import view as chart_view
    from tuner_under_test.const import HISTORY_KEYS
    from tuner_under_test.worker import bridge, calculations, protocol

    source = chart_view([(0.0, row(10)), (123.125, row(90)), (123.175, row(10))], end=86400)
    source.data["devices"]["a"]["history_labels"] = [{"start": 100, "end": 200, "state": "present"}]
    parameters = {
        "device_id": "a",
        "keys": list(HISTORY_KEYS[:gate_count]),
        "hours": 24,
        "max_points": max_points,
        "end": 86400,
    }
    holder = {}
    with ThreadPoolExecutor() as executor:
        path, *_ = executor.submit(
            bridge._build, holder, "charts", {"views": {"a": source}, "parameters": parameters}
        ).result()
    try:
        from pathlib import Path

        result = calculations.execute(protocol.unpack(Path(path).read_bytes(), "learn"))
        restored = display.unpack_chart(codec.unpack_result(codec.pack_result(result)))
        from tuner_under_test.presentation.charts import history_series_multi

        assert restored == history_series_multi(source, **parameters)
        assert restored["start"] == 0
        assert restored["end"] == 86400
        for series in restored["series"].values():
            assert series["observed_seconds"] == 86400
            assert series["points"][0]["t"] == 0
            assert series["points"][-1]["t"] == 86400
    finally:
        import shutil

        shutil.rmtree(holder["directory"])


@pytest.mark.parametrize("failure", ["truncated", "trailing", "expansion", "shape", "nonfinite"])
def test_chart_transport_rejects_invalid_payloads(monkeypatch, failure):
    import json
    import zlib

    raw = b"{}"
    if failure == "shape":
        raw = json.dumps({"series": []}).encode()
    if failure == "nonfinite":
        raw = b'{"start":NaN}'
    compressed = zlib.compress(raw)
    if failure == "truncated":
        compressed = compressed[:-1]
    if failure == "trailing":
        compressed += b"trailing"
    if failure == "expansion":
        monkeypatch.setattr(display, "CHART_RAW_BYTES", 1)
    with pytest.raises(ValueError):
        display.unpack_chart({"chart_zlib": base64.b64encode(compressed).decode("ascii")})


def test_async_chart_captures_requested_window_and_expands_outside_event_loop(monkeypatch):
    import asyncio
    import threading
    from collections import OrderedDict
    from types import SimpleNamespace

    from test_event_charts import chart, row
    from test_event_charts import view as chart_view
    from tuner_under_test.presentation import charts
    from tuner_under_test.worker import bridge

    async def run():
        source = chart_view([(0.0, row(10))], end=86400)
        expected = chart(source, end=86400, hours=1)
        source._history_cache, source._history_jobs = OrderedDict(), {}
        source._history_view = lambda *args, **kwargs: source
        source.hass = SimpleNamespace(async_add_executor_job=asyncio.to_thread)
        main_thread, expanded = threading.get_ident(), []
        unpack = display.unpack_chart

        def expand(value):
            expanded.append(threading.get_ident())
            return unpack(value)

        async def calculate(runtime, operation, prepare):
            prepared = prepare()
            captured = prepared["views"]["a"]
            assert captured._snapshot_history_start == 82800
            assert captured._history_until == 86400
            return display.pack_chart(expected)

        monkeypatch.setattr(bridge, "calculate", calculate)
        monkeypatch.setattr(display, "unpack_chart", expand)
        result = await charts.async_history_series(source, "a", ["g0_move"], 1, 50, 86400)
        assert result == expected
        assert expanded and all(thread != main_thread for thread in expanded)
        assert await charts.async_history_series(source, "a", ["g0_move"], 1, 50, 86400) == result
        assert len(expanded) == 1

    asyncio.run(run())


def test_display_export_detached_is_lossless(tmp_path):
    from tuner_under_test.worker import calculations

    source = view([block(0, 600, 40)], [])
    with ThreadPoolExecutor() as executor:
        request = executor.submit(capture.prepare_multi, {"any-name": source}, tmp_path).result()
    request.update(operation="export_days", parameters=codec.encode({"device_id": "any-name"}))
    actual = calculations.execute(request)
    assert list(actual) == ["1970-01-01"]
    decoded = dayfile.decode_day(base64.b64decode(actual["1970-01-01"]))
    assert list(decoded.times) == [0]
    assert list(decoded.ends) == [600]
    assert list(decoded.columns[0]) == [40]


def test_export_size_preflight_rejects_metadata_before_copy():
    from tuner_under_test.worker.display import check_export

    with pytest.raises(ValueError, match="budget"):
        check_export({"last_learning": {"payload": "x" * (1024 * 1024 + 1)}})
    with pytest.raises(ValueError, match="budget"):
        check_export({"last_learning": ["x" * 600000, "y" * 600000]})


def test_export_size_allows_tuple_metadata():
    from tuner_under_test.worker.display import check_export

    check_export({"last_learning": {"interval": (0, 600)}})


def test_async_chart_requires_process_transport():
    import asyncio
    from collections import OrderedDict

    from tuner_under_test.presentation import charts
    from tuner_under_test.worker import bridge, supervisor

    async def run():
        source = view([block(0, 600, 40)], [])
        source._history_cache, source._history_jobs = OrderedDict(), {}
        acquired = []
        source._history_view = lambda *args, **kwargs: acquired.append(True) or source
        await bridge.stop(source)
        with pytest.raises(supervisor.Closed, match="stopped"):
            await charts.async_history_series(source, "any-name", ["g0_move"], 1, 100, 1200)
        assert acquired == []

    asyncio.run(run())


def test_async_export_rejects_large_metadata_before_registry_access():
    import asyncio
    from unittest.mock import patch

    from tuner_under_test.presentation import snapshots

    async def run():
        source = view([], [])
        source.data["devices"]["any-name"]["last_learning"] = {"large": "x" * (1024 * 1024 + 1)}
        with patch.object(
            snapshots.er, "async_get", side_effect=AssertionError("registry accessed"), create=True
        ):
            with pytest.raises(ValueError, match="budget"):
                await snapshots.async_export_data(source, "any-name")

    asyncio.run(run())


def test_export_day_budget_rejects_instead_of_falling_back(monkeypatch):
    from tuner_under_test.worker import display

    source = view([block(0, 600, 40)], [])
    monkeypatch.setattr(display, "EXPORT_BYTES", 10)
    with pytest.raises(ValueError, match="Export day data.*budget"):
        display.recorded_days(source, "any-name")


def test_export_missing_day_store_is_empty():
    from types import SimpleNamespace

    from tuner_under_test.worker.display import recorded_days

    assert recorded_days(SimpleNamespace(_days=None), "a") == {}


@pytest.mark.parametrize("value", [[None] * 32769, [[[[None]]]]])
def test_export_rejects_excess_nodes_or_depth(monkeypatch, value):
    from tuner_under_test.worker import display

    monkeypatch.setattr(display, "EXPORT_NODES", 4)
    with pytest.raises(ValueError, match="budget"):
        display.check_export(value)


@pytest.mark.parametrize("operation", ["charts", "summary", "export_days"])
def test_display_runs_in_independent_python_without_homeassistant(tmp_path, operation):
    import json
    import subprocess
    import sys
    from pathlib import Path

    from tuner_under_test.const import HISTORY_KEYS
    from tuner_under_test.worker import calculations, protocol

    source = view([block(0, 86400 if operation in ("charts", "summary") else 600, 40)], [])
    with ThreadPoolExecutor() as executor:
        request = executor.submit(capture.prepare_multi, {"any-name": source}, tmp_path).result()
    parameters = {"device_id": "any-name"}
    if operation in ("charts", "summary"):
        parameters.update(keys=list(HISTORY_KEYS), hours=24, max_points=1000, end=86400)
    if operation == "summary":
        parameters["summary"] = True
        operation = "charts"
    request.update(operation=operation, parameters=codec.encode(parameters))
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
            "assert 'homeassistant' not in sys.modules",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", script, str(path)],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert codec.unpack_result(result.stdout) == calculations.execute(request)


def test_chart_transport_rejects_malformed_geometry_and_envelopes():
    """Corrupt child output cannot become an unbounded or invalid HA chart."""
    import copy

    from tuner_under_test.presentation.charts import history_series_multi

    valid = history_series_multi(view([block(0, 600, 40)], []), "any-name", ["g0_move"], 1, end=600)
    changes = [
        (("end",), -4000),
        (("basis",), "unrecognised"),
        (("series",), []),
        (("series",), {"foreign_gate": valid["series"]["g0_move"]}),
        (("labels",), [{}]),
        (("labels",), [{"start": 2, "end": 1, "state": "present"}]),
        (("series", "g0_move"), {}),
        (("series", "g0_move", "basis"), "unrecognised"),
        (("series", "g0_move", "points"), [None]),
        (("series", "g0_move", "points"), [None] * 1001),
        (("series", "g0_move", "points", 0, "anchor"), False),
        (("series", "g0_move", "points", 0, "avg"), float("inf")),
        (("series", "g0_move", "gaps"), [[1]]),
        (("series", "g0_move", "gaps"), [[2, 1]]),
    ]
    for path, value in changes:
        damaged = copy.deepcopy(valid)
        target = damaged
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        with pytest.raises(ValueError):
            display.pack_chart(damaged)
    for envelope in (None, {}, {"chart_zlib": 1}, {"chart_zlib": "!"}, {"chart_zlib": "AAAA"}):
        with pytest.raises(ValueError):
            display.unpack_chart(envelope)


def test_chart_transport_enforces_compressed_and_expanded_byte_limits(monkeypatch):
    from tuner_under_test.presentation.charts import history_series_multi

    valid = history_series_multi(view([block(0, 600, 40)], []), "any-name", ["g0_move"], 1, end=600)
    for name in ("CHART_RAW_BYTES", "CHART_WIRE_BYTES"):
        with monkeypatch.context() as patch:
            patch.setattr(display, name, 1)
            with pytest.raises(ValueError):
                display.pack_chart(valid)


def test_room_summary_preserves_simultaneous_geometry_and_full_chart(tmp_path):
    from test_event_charts import chart, row
    from test_event_charts import view as chart_view
    from tuner_under_test.worker import calculations

    source = chart_view(
        [],
        blocks=[
            ([(0, row(10, 10)), (50.125, row(90, 20)), (50.175, row(None, 20))], 100),
            ([(100.125, row(30, None)), (110, row(30, 40))], 600),
        ],
    )
    parameters = {
        "device_id": "a",
        "keys": ["g0_move", "g0_still"],
        "hours": 1,
        "max_points": 50,
        "end": 600,
    }
    full = chart(source, keys=parameters["keys"])
    simultaneous = chart_view(
        [],
        blocks=[
            ([(0, row(10)), (50.125, row(90)), (50.175, row(20))], 100),
            ([(100.125, row(30)), (110, row(40))], 600),
        ],
    )
    expected_native = chart(simultaneous)
    expected = {key: expected_native[key] for key in ("start", "end", "bucket_seconds")}
    expected["points"] = expected_native["series"]["g0_move"]["points"]
    with ThreadPoolExecutor() as executor:
        request = executor.submit(capture.prepare_multi, {"a": source}, tmp_path).result()
    for summary in (False, True):
        request.update(
            operation="charts", parameters=codec.encode({**parameters, "summary": summary})
        )
        assert display.unpack_chart(calculations.execute(request)) == (
            expected if summary else full
        )


def test_summary_chart_shape_budget_and_empty():
    from copy import deepcopy

    from tuner_under_test.presentation.charts import history_series_multi

    full = history_series_multi(view([block(0, 600, 40)], []), "any-name", ["g0_move"], 1, end=600)
    summary = {key: full[key] for key in ("start", "end", "bucket_seconds")}
    summary["points"] = []
    assert display.unpack_chart(display.pack_chart(summary)) == summary
    point = full["series"]["g0_move"]["points"][0]
    summary["points"] = [point] * 18000
    assert len(display.unpack_chart(display.pack_chart(summary))["points"]) == 18000
    for mutation in ({"points": [point] * 18001}, {"extra": 1}, {"points": [{}]}):
        damaged = {**deepcopy(summary), **mutation}
        with pytest.raises(ValueError):
            display.pack_chart(damaged)


def test_room_summary_empty_and_gate_order_are_semantically_stable():
    from test_event_charts import row
    from test_event_charts import view as chart_view

    parameters = {"device_id": "a", "hours": 1, "max_points": 50, "end": 600, "summary": True}
    source = chart_view([(0, row(10, 10)), (100, row(20, 40))])
    forward = display.unpack_chart(
        display.calculate("charts", source, {**parameters, "keys": ["g0_move", "g0_still"]})
    )
    reverse = display.unpack_chart(
        display.calculate("charts", source, {**parameters, "keys": ["g0_still", "g0_move"]})
    )
    assert forward == reverse
    empty = display.unpack_chart(
        display.calculate("charts", chart_view([], blocks=[]), {**parameters, "keys": ["g0_move"]})
    )
    assert empty["points"] == []


def test_async_room_summary_cache_keeps_mode_and_gate_order(monkeypatch):
    import asyncio
    from collections import OrderedDict
    from types import SimpleNamespace

    from test_event_charts import row
    from test_event_charts import view as chart_view
    from tuner_under_test.presentation import charts
    from tuner_under_test.worker import bridge

    async def run():
        source = chart_view([(0, row(10, 10))])
        source._history_cache, source._history_jobs = OrderedDict(), {}
        source._history_view = lambda *args, **kwargs: source
        source.hass = SimpleNamespace(async_add_executor_job=asyncio.to_thread)
        calls = []

        async def calculate(runtime, operation, prepare):
            prepared = prepare()
            assert prepared["views"]["a"]._snapshot_history_start == -3000
            calls.append(prepared["parameters"])
            return display.calculate(operation, source, prepared["parameters"])

        monkeypatch.setattr(bridge, "calculate", calculate)
        keys = ["g0_move", "g0_still"]
        for summary, requested in (
            (False, keys),
            (True, keys),
            (True, keys[::-1]),
            (True, keys),
            (False, keys[::-1]),
        ):
            result = await charts.async_history_series(
                source, "a", requested, 1, 50, 600, summary=summary
            )
            assert ("points" in result) is summary
        assert len(calls) == 3
        assert [call["summary"] for call in calls] == [False, True, True]
        assert calls[2]["keys"] == keys[::-1]

    asyncio.run(run())


def _dense_chart():
    from unittest.mock import patch

    from test_event_charts import _blocks_view
    from tuner_under_test.presentation.charts import history_series_multi

    end = 3000.0
    changes = [
        (i * 0.5, {gate: (i * 7 + gate * 13) % 97 for gate in range(18)}) for i in range(6000)
    ]
    runtime = _blocks_view([(changes, end)], end)
    keys = [f"g{gate}_still" for gate in range(9)]
    with patch("time.time", return_value=end):
        return history_series_multi(runtime, "a", keys, end / 3600, 400, end)


def test_chart_transport_rejects_integers_beyond_64_bits():
    import copy
    import json
    import zlib

    valid = _dense_chart()
    damaged = copy.deepcopy(valid)
    next(iter(damaged["series"].values()))["points"][0]["count"] = 2**64
    with pytest.raises(ValueError):
        display.pack_chart(damaged)
    raw = json.dumps(damaged, separators=(",", ":")).encode()
    with pytest.raises(ValueError):
        display.unpack_chart({"chart_zlib": base64.b64encode(zlib.compress(raw)).decode("ascii")})

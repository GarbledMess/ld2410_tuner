"""Pure display jobs retain chart geometry and lossless bounded export days."""

import base64
from concurrent.futures import ThreadPoolExecutor

import pytest
import test_tuner  # noqa: F401
from test_event_fitting import view
from test_event_quality import block
from tuner_under_test.history import dayfile
from tuner_under_test.worker import capture, codec


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
    actual = calculations.execute(request)
    assert actual == expected


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


@pytest.mark.parametrize("operation", ["charts", "export_days"])
def test_display_runs_in_independent_python_without_homeassistant(tmp_path, operation):
    import json
    import subprocess
    import sys
    from pathlib import Path

    from tuner_under_test.worker import calculations, protocol

    source = view([block(0, 600, 40)], [])
    with ThreadPoolExecutor() as executor:
        request = executor.submit(capture.prepare_multi, {"any-name": source}, tmp_path).result()
    parameters = {"device_id": "any-name"}
    if operation == "charts":
        parameters.update(keys=["g0_move"], hours=0.5, max_points=100, end=1200)
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

"""Each radar remembers every gate's energy from the frames where it found and lost presence."""

import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "examples/esphome/ld2410c.yaml"
# const.py needs no Home Assistant; importing it through the integration package would.
_spec = importlib.util.spec_from_file_location(
    "ld2410_const", ROOT / "custom_components/ld2410_tuner/const.py"
)
_const = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_const)
GATE_RE = _const.GATE_RE
GATES = [(gate, kind) for gate in range(9) for kind in ("move", "still")]
EDGES = ("found", "lost")
SCRIPT = "ld2410_capture_presence_edge"


def _package():
    return yaml.load(PACKAGE.read_text().replace("${ld2410_id}", "ld2410"), Loader=yaml.BaseLoader)


def _snapshots(package):
    return {
        sensor["id"]: sensor
        for sensor in package["sensor"]
        if sensor.get("id", "").endswith(("_found", "_lost"))
    }


def _capture(package):
    return next(script for script in package["script"] if script["id"] == SCRIPT)


def test_found_and_lost_snapshot_sensors_are_inert_diagnostics():
    snapshots = _snapshots(_package())
    assert len(snapshots) == 36
    for gate, kind in GATES:
        for edge in EDGES:
            sensor = snapshots[f"ld2410_g{gate}_{kind}_{edge}"]
            assert sensor["platform"] == "template"
            assert sensor["name"] == f"G{gate} {kind.title()} at Presence {edge.title()}"
            assert sensor["entity_category"] == "diagnostic"
            assert sensor["accuracy_decimals"] == "0"
            assert sensor["update_interval"] == "never"
            assert sensor["device_id"] == "${ld2410_device_id}"
            assert "lambda" not in sensor
            assert "filters" not in sensor


def test_snapshot_entity_ids_are_not_mistaken_for_live_gates():
    entity_ids = _snapshots(_package())
    assert len(entity_ids) == 36
    for entity_id in entity_ids:
        assert GATE_RE.match(entity_id) is None, entity_id


def test_both_unfiltered_target_sensors_run_the_capture_after_the_frame():
    package = _package()
    radar = package["binary_sensor"][0]
    for target in ("has_moving_target", "has_still_target"):
        assert radar[target]["on_state"] == [{"script.execute": SCRIPT}]
        # ESPHome skips on_state for the first state by default; the boot frame must seed
        # the global or the first real edge after boot only seeds it and is lost.
        assert radar[target]["trigger_on_initial_state"] == "true"
        assert "filters" not in radar[target]
    script = _capture(package)
    assert script["mode"] == "restart"
    assert script["then"][0] == {"delay": "0s"}
    assert "lambda" in script["then"][1]
    memory = next(item for item in package["globals"] if item["id"] == "ld2410_last_presence")
    assert (memory["type"], memory["initial_value"], memory["restore_value"]) == (
        "int",
        "-1",
        "false",
    )


def test_capture_publishes_raw_gate_values_only_on_a_real_edge():
    code = " ".join(_capture(_package())["then"][1]["lambda"].split())
    assert "id(ld2410_moving_target).state || id(ld2410_still_target).state" in code
    remembered = code.index("id(ld2410_last_presence) = present;")
    assert remembered < code.index("if (previous < 0 || previous == present) return;")
    assert "get_raw_state()" in code
    assert "std::isfinite(" in code
    for gate, kind in GATES:
        for name in ("energy", *EDGES):
            assert f"id(ld2410_g{gate}_{kind}_{name})" in code

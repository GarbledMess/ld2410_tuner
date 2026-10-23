"""Each radar remembers which gates were over threshold when it found and lost presence."""

from pathlib import Path

import yaml

PACKAGE = Path(__file__).resolve().parents[1] / "examples/esphome/ld2410c.yaml"
GATES = [(gate, kind) for gate in range(9) for kind in ("move", "still")]
EDGES = ("found", "lost")
SCRIPT = "ld2410_capture_presence_edge"


def _package():
    return yaml.load(PACKAGE.read_text().replace("${ld2410_id}", "ld2410"), Loader=yaml.BaseLoader)


def _capture(package):
    return next(script for script in package["script"] if script["id"] == SCRIPT)


def _code():
    return " ".join(_capture(_package())["then"][1]["lambda"].split())


def test_per_gate_snapshot_values_are_replaced_by_the_two_lists():
    ids = [sensor.get("id", "") for sensor in _package()["sensor"]]
    assert not [i for i in ids if i.endswith(("_found", "_lost"))]


def test_found_and_lost_gate_lists_are_inert_diagnostic_text_sensors():
    texts = {item.get("id"): item for item in _package()["text_sensor"]}
    for edge in EDGES:
        sensor = texts[f"ld2410_presence_{edge}_gates"]
        assert sensor["platform"] == "template"
        assert sensor["name"] == f"Presence {edge.title()} Gates"
        assert sensor["entity_category"] == "diagnostic"
        assert sensor["update_interval"] == "never"
        assert sensor["device_id"] == "${ld2410_device_id}"
        assert "lambda" not in sensor


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


def test_capture_judges_raw_gate_values_only_on_a_real_edge():
    code = _code()
    assert "id(ld2410_moving_target).state || id(ld2410_still_target).state" in code
    remembered = code.index("id(ld2410_last_presence) = present;")
    assert remembered < code.index("if (previous < 0 || previous == present) return;")
    assert "get_raw_state()" in code
    for gate, kind in GATES:
        assert f"id(ld2410_g{gate}_{kind}_energy)" in code
        assert f"id(ld2410_g{gate}_{kind}_raw)" in code
    assert "id(ld2410_max_move)" in code
    assert "id(ld2410_max_still)" in code


def test_each_edge_lists_its_gates_over_threshold_with_value_and_threshold():
    code = _code()
    assert (
        "auto *list = present ? id(ld2410_presence_found_gates) : id(ld2410_presence_lost_gates);"
        in code
    )
    assert "if (!present) return;" not in code
    # Engineering Mode off leaves the previous list in place.
    assert code.index("if (!captured) return;") < code.index("list->publish_state(")
    assert 'list->publish_state("Settings unavailable");' in code
    assert '"None over threshold"' in code
    # Each entry reads like "G3 Move 45>30": the frame's value, then the threshold it beat.
    assert '(i % 2 ? " Still " : " Move ")' in code
    assert (
        'std::to_string(static_cast<int>(value)) + ">" + std::to_string(static_cast<int>(threshold))'
        in code
    )
    # Equal to the threshold does not detect, as in the tuner's own replay.
    assert "!(value > threshold)" in code
    assert ">= threshold" not in code


def test_gate_list_stays_within_the_home_assistant_state_limit():
    code = _code()
    # 18 entries can exceed 255 characters; the rest are counted instead of rejected by HA.
    assert "255" in code
    assert '" more"' in code

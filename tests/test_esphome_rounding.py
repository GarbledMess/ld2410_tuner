"""Rounding is owned by each ESP package, including threshold write boundaries."""

from pathlib import Path

import yaml

PACKAGE = Path(__file__).resolve().parents[1] / "examples/esphome/ld2410c.yaml"


def test_all_energy_sensors_publish_whole_percentages():
    package = yaml.load(PACKAGE.read_text(), Loader=yaml.BaseLoader)
    radar = package["sensor"][0]
    energies = [radar["moving_energy"], radar["still_energy"]]
    energies.extend(
        radar[f"g{gate}"][f"{kind}_energy"] for gate in range(9) for kind in ("move", "still")
    )
    for sensor in energies:
        assert sensor["accuracy_decimals"] == "0"
        code = next(f["lambda"] for f in sensor["filters"] if "lambda" in f)
        assert "std::isfinite(x)" in code
        assert "return ceilf(" in code


def test_gate_rounding_default_and_all_energy_filters():
    package = yaml.load(PACKAGE.read_text(), Loader=yaml.BaseLoader)
    assert package["substitutions"]["ld2410_gate_rounding"] == "10"
    radar = package["sensor"][0]
    for gate in range(9):
        for kind in ("move", "still"):
            filters = radar[f"g{gate}"][f"{kind}_energy"]["filters"]
            assert "id(${ld2410_id}_gate_rounding).state" in filters[1]["lambda"]
            assert "std::isfinite(x)" in filters[1]["lambda"]
            assert filters[2] == {"delta": "1"}


def test_threshold_controls_round_before_writing_and_read_actual_values():
    package = yaml.load(PACKAGE.read_text(), Loader=yaml.BaseLoader)
    numbers = {n["id"]: n for n in package["number"] if n["platform"] == "template"}
    for gate in range(9):
        for kind in ("move", "still"):
            key = f"${{ld2410_id}}_g{gate}_{kind}"
            raw = package["number"][0][f"g{gate}"][f"{kind}_threshold"]
            assert raw["id"] == key + "_raw"
            assert raw["internal"] == "true"
            assert raw["on_value"][0]["lambda"] == f"id({key}).publish_state(x);"
            control = numbers[key]
            assert control["step"] == "${ld2410_gate_rounding}"
            assert control["lambda"] == f"return id({key}_raw).state;"
            assert control["update_interval"] == "never"
            action = control["set_action"][0]["number.set"]
            assert action["id"] == key + "_raw"
            for attribute in ("min_value", "max_value", "step"):
                assert f"id({key}).traits.get_{attribute}()" in action["value"]
            assert "roundf((x - minimum) / increment)" in action["value"]
            assert "floorf((maximum - minimum) / increment)" in action["value"]


def test_runtime_rounding_is_restored_and_refreshes_metadata_without_radar_writes():
    package = yaml.load(PACKAGE.read_text(), Loader=yaml.BaseLoader)
    control = next(n for n in package["number"] if n.get("name") == "Gate Rounding")
    assert control["initial_value"] == "${ld2410_gate_rounding}"
    assert control["restore_value"] == "true"
    assert (control["min_value"], control["max_value"], control["step"]) == ("1", "100", "1")
    code = control["on_value"][0]["lambda"]
    for gate in range(9):
        for kind in ("move", "still"):
            assert f"id(${{ld2410_id}}_g{gate}_{kind})" in code
    assert "traits.set_step" in code
    assert "configured_steps[i] == ${ld2410_gate_rounding}" in code
    assert "if (changed)" in code
    assert "_refresh_rounding_metadata).execute()" in code
    assert "number.set" not in code
    refresh = next(s for s in package["script"] if "refresh_rounding_metadata" in s["id"])
    assert refresh["mode"] == "restart"
    assert refresh["then"][0] == {"delay": "500ms"}
    assert refresh["then"][1] == {"delay": "${ld2410_gate_throttle}"}
    assert "gate->publish_state(gate->get_raw_state())" in refresh["then"][2]["lambda"]
    assert "DisconnectRequest" in refresh["then"][-1]["lambda"]


def test_public_gate_thresholds_are_configuration_not_dashboard_controls():
    """Room dashboards list primary entities; thresholds belong on the device's settings."""
    package = yaml.load(PACKAGE.read_text(), Loader=yaml.BaseLoader)
    thresholds = [
        number
        for number in package["number"]
        if number.get("platform") == "template" and number.get("name", "").endswith("Threshold")
    ]
    assert len(thresholds) == 18
    for number in thresholds:
        assert number.get("entity_category") == "config", number["name"]

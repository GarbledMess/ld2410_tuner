"""Combined Gate Report: one Gate Energies state per radar frame, off unless switched on.

Off (the default), the package reports exactly as before. On, Gate Energies carries every gate
change (sampled like the gate sensors) and each gate sensor refreshes only slowly.
"""

from pathlib import Path

import yaml

PACKAGE = Path(__file__).resolve().parents[1] / "examples/esphome/ld2410c.yaml"
GATES = [(gate, kind) for gate in range(9) for kind in ("move", "still")]


def _package():
    return yaml.load(PACKAGE.read_text().replace("${ld2410_id}", "ld2410"), Loader=yaml.BaseLoader)


def _code(text):
    return " ".join(text.split())


def test_refresh_interval_is_a_substitution():
    assert _package()["substitutions"]["ld2410_combined_gate_refresh"] == "300"


def test_switch_is_a_restored_config_switch_that_starts_off():
    switch = next(s for s in _package()["switch"] if s.get("id") == "ld2410_combined_gates")
    assert switch["platform"] == "template"
    assert switch["name"] == "${ld2410_name_prefix}Combined Gate Report"
    assert switch["device_id"] == "${ld2410_device_id}"
    assert switch["entity_category"] == "config"
    assert switch["optimistic"] == "true"
    assert switch["restore_mode"] == "RESTORE_DEFAULT_OFF"
    # Off: Gate Energies carries no frame, so readers fall back to the gate sensors.
    assert switch["on_turn_off"] == [
        {"text_sensor.template.publish": {"id": "ld2410_gate_energies", "state": ""}}
    ]


def test_gate_energies_is_an_inert_diagnostic_text_sensor():
    texts = {t.get("id"): t for t in _package()["text_sensor"]}
    sensor = texts["ld2410_gate_energies"]
    assert sensor["platform"] == "template"
    assert sensor["name"] == "${ld2410_name_prefix}Gate Energies"
    assert sensor["device_id"] == "${ld2410_device_id}"
    assert sensor["entity_category"] == "diagnostic"
    assert sensor["update_interval"] == "never"
    assert "lambda" not in sensor


def _report():
    intervals = _package()["interval"]
    found = [i for i in intervals if "ld2410_gate_energies" in str(i)]
    assert len(found) == 1
    return found[0]


def test_report_samples_at_the_gate_throttle_and_publishes_only_changes():
    report = _report()
    assert report["interval"] == "${ld2410_gate_throttle}"
    code = _code(report["then"][0]["lambda"])
    assert code.startswith("if (!id(ld2410_combined_gates).state) return;")
    for gate, kind in GATES:
        assert f"id(ld2410_g{gate}_{kind}_energy)," in code or (
            f"id(ld2410_g{gate}_{kind}_energy) }}" in code
        )
    assert "get_raw_state()" in code
    assert "if (i) report += ',';" in code
    assert "if (!std::isfinite(raw)) continue;" in code
    # The gate sensors' own rounding, from the same Gate Rounding control.
    assert "id(ld2410_gate_rounding).state : ${ld2410_gate_rounding}" in code
    assert "ceilf(fminf(100.0f, fmaxf(0.0f, roundf(raw / increment) * increment)))" in code
    assert (
        "if (report != id(ld2410_gate_energies).state) "
        "id(ld2410_gate_energies).publish_state(report);"
    ) in code


def test_each_gate_sensor_refreshes_slowly_while_combined_and_reports_unknown_at_once():
    radar = _package()["sensor"][0]
    for gate, kind in GATES:
        sensor = radar[f"g{gate}"][f"{kind}_energy"]
        filters = sensor["filters"]
        assert list(filters[0]) == ["throttle"]
        assert filters[2] == {"delta": "1"}  # After the refresh, so a skipped change shows.
        code = _code(filters[1]["lambda"])
        unknown = code.index("if (!std::isfinite(x)) return x;")
        refresh = code.index("static uint32_t refreshed = 0;")
        assert unknown < refresh
        assert (
            f"if (id(ld2410_combined_gates).state && std::isfinite(id(ld2410_g{gate}_{kind}_energy).state)"
            " && millis() - refreshed < ${ld2410_combined_gate_refresh} * 1000UL) return {};"
        ) in code
        assert code.index("refreshed = millis();") < code.index("return ceilf(")


SUMMARY = (
    "moving_distance",
    "still_distance",
    "moving_energy",
    "still_energy",
    "detection_distance",
)


def test_summary_sensors_can_report_only_when_presence_changes_and_do_not_by_default():
    package = _package()
    assert package["substitutions"]["ld2410_summary_on_presence_change"] == "false"
    radar = package["sensor"][0]
    for name in SUMMARY:
        filters = radar[name]["filters"]
        assert filters[0] == {"throttle": "${ld2410_sensor_throttle}"}
        code = _code(filters[-1]["lambda"])
        # Off: every throttled reading passes, exactly as before.
        assert code.startswith("if (!${ld2410_summary_on_presence_change}) return x;")
        # On: one reading per radar presence change (the unfiltered targets, as the
        # Presence Found/Lost Gates capture reads them), held until the next change.
        assert "static int reported = -1;" in code
        assert (
            "const int present = id(ld2410_moving_target).state || id(ld2410_still_target).state;"
        ) in code
        assert "if (present == reported) return {};" in code
        assert code.endswith("reported = present; return x;")
    # The energies keep their whole-percentage rounding ahead of it.
    for name in ("moving_energy", "still_energy"):
        assert "return ceilf(" in radar[name]["filters"][1]["lambda"]


def test_presence_is_unchanged():
    assert "combined" not in str(_package()["binary_sensor"])
    assert "summary" not in str(_package()["binary_sensor"])

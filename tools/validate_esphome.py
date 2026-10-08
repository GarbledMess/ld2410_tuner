"""Validate radar isolation with ESPHome itself, without connecting to hardware.

Run in an environment with ESPHome 2026.9.1 installed. All generated files stay
in a temporary directory. This does not compile or flash firmware.
"""

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

PACKAGE = Path(__file__).resolve().parents[1] / "examples/esphome/ld2410c.yaml"
OVERRIDE_PRESENCE_NAME = "Existing Presence"
DOMAINS = ("binary_sensor", "sensor", "number", "select", "switch", "text_sensor", "button")


def _node(count, override=False):
    node = {
        "esphome": {"name": f"radar-check-{count}", "friendly_name": "Radar Check"},
        "logger": {"baud_rate": 0},
        "api": {},
    }
    if count > 3:
        node["host"] = {}
    else:
        node["esp32"] = {
            "board": "esp32dev" if count == 3 else "esp32-c3-devkitm-1",
            "framework": {"type": "esp-idf"},
        }
        node["wifi"] = {"ssid": "test-only", "password": "test-only"}
    if count == 1:
        node["uart"] = {"tx_pin": "GPIO4", "rx_pin": "GPIO3"}
    else:
        node["uart"] = [_bus(i, count) for i in range(count)]
        node["esphome"]["devices"] = [
            {"id": f"device_{i}", "name": f"Radar {i}"} for i in range(1, count)
        ]
    if override:
        node["uart"].update(
            id="existing_bus", tx_pin="GPIO6", rx_pin="GPIO7", baud_rate=115200, rx_buffer_size=2048
        )
        node["substitutions"] = {
            "ld2410_presence_name": OVERRIDE_PRESENCE_NAME,
            "ld2410_presence_on_delay": "3s",
            "ld2410_presence_off_delay": "10s",
            "ld2410_sensor_throttle": "3s",
            "ld2410_gate_throttle": "4s",
        }
    return node


def _bus(index, count):
    bus = {"id": f"bus_{index}", "baud_rate": 256000, "rx_buffer_size": 1024}
    if count > 3:
        bus["port"] = f"/dev/ttyRADAR{index}"
        bus["baud_rate"] = 115200  # Supported host UART rate; radars can be configured to match.
    else:
        pins = ((4, 3), (6, 7)) if count == 2 else ((16, 17), (25, 26), (32, 33))
        bus.update(tx_pin=f"GPIO{pins[index][0]}", rx_pin=f"GPIO{pins[index][1]}")
    return bus


def _includes(count):
    if count == 1:
        return f"packages:\n  ld2410: !include {PACKAGE}\n"
    lines = ["packages:"]
    for index in range(count):
        variables = {}
        if count > 1:
            variables["ld2410_uart_id"] = f"bus_{index}"
        if index:
            variables.update(
                ld2410_id=f"radar_{index}",
                ld2410_device_id=f"device_{index}",
                ld2410_presence_on_delay=f"{index + 1}s",
            )
        include = {"file": str(PACKAGE), "vars": variables}
        lines.append(f"  radar_{index}: !include {json.dumps(include)}")
    return "\n".join(lines) + "\n"


def _entities(value):
    if isinstance(value, dict):
        if "name" in value and not value.get("internal"):
            yield value
        for child in value.values():
            yield from _entities(child)
    elif isinstance(value, list):
        for child in value:
            yield from _entities(child)


def _check(config, count, expected_names, override=False):
    assert len(config["ld2410"]) == count
    assert len(config["uart"]) == count
    assert len(config["script"]) == count
    assert len(config["globals"]) == 4 * count
    for index, radar in enumerate(config["ld2410"]):
        prefix = f"radar_{index}" if index else "ld2410"
        assert str(radar["id"]) == f"{prefix}_radar"
        assert str(radar["uart_id"]) == str(config["uart"][index]["id"])
        device = f"device_{index}" if index else "None"
        for domain in DOMAINS:
            entities = [e for e in _entities(config[domain]) if str(e.get("device_id")) == device]
            names = _expected_names(expected_names, domain, override)
            assert Counter(e["name"] for e in entities) == names, (index, domain)
            assert all(str(e["id"]).startswith(f"{prefix}_") for e in entities if e["name"])


def _expected_names(expected_names, domain, override):
    names = expected_names[domain].copy()
    if override and domain == "binary_sensor":
        names[OVERRIDE_PRESENCE_NAME] = names.pop("Presence")
    return names


def main():
    # Import only for this explicit firmware validation; HA tests stub other modules.
    from esphome.config import read_config
    from esphome.core import CORE

    expected_names = None
    with TemporaryDirectory(prefix="ld2410-package-") as directory:
        for count, override in ((1, False), (1, True), (2, False), (3, False), (12, False)):
            path = Path(directory) / f"radars-{count}-{override}.yaml"
            path.write_text(yaml.safe_dump(_node(count, override)) + _includes(count))
            CORE.reset()
            CORE.config_path = path
            config = read_config({})
            assert config is not None, f"ESPHome rejected {path.name}"
            if expected_names is None:
                expected_names = {
                    d: Counter(e["name"] for e in _entities(config[d])) for d in DOMAINS
                }
            _check(config, count, expected_names, override)
            _check_options(config, count, override)
            subprocess.run(
                [sys.executable, "-m", "esphome", "compile", "--only-generate", str(path)],
                check=True,
            )
            print(f"Validated {count} radar(s), overrides={override}; generated C++", flush=True)


def _check_options(config, count, override):
    presence = config["binary_sensor"][0]["has_target"]
    assert presence["filters"][0]["delayed_on"].total_milliseconds == (3000 if override else 500)
    assert config["uart"][0]["baud_rate"] == (115200 if override or count > 3 else 256000)
    assert config["uart"][0]["rx_buffer_size"] == (2048 if override else 1024)
    if count == 1:
        _check_legacy_options(config, presence, override)
    energies = {f"G{gate} {kind} Energy" for gate in range(9) for kind in ("Move", "Still")}
    assert energies <= {e["name"] for e in _entities(config["sensor"])}
    for index in range(1, count):
        sensor = next(
            x for x in config["binary_sensor"] if str(x.get("ld2410_id")) == f"radar_{index}_radar"
        )
        assert (
            sensor["has_target"]["filters"][0]["delayed_on"].total_milliseconds
            == (index + 1) * 1000
        )


def _check_legacy_options(config, presence, override):
    assert presence["name"] == (OVERRIDE_PRESENCE_NAME if override else "Presence")
    assert presence["filters"][1]["delayed_off"].total_milliseconds == (10000 if override else 1000)
    bus = config["uart"][0]
    assert bus["tx_pin"]["number"] == (6 if override else 4)
    assert bus["rx_pin"]["number"] == (7 if override else 3)
    sensor = config["sensor"][0]
    assert sensor["moving_energy"]["filters"][0]["throttle"].total_milliseconds == (
        3000 if override else 1000
    )
    assert sensor["g0"]["still_energy"]["filters"][0]["throttle"].total_milliseconds == (
        4000 if override else 2000
    )
    assert str(config["script"][0]["id"]) == "ld2410_recover_parameters"
    assert str(config["ld2410"][0]["id"]) == "ld2410_radar"
    assert not config["esphome"]["devices"]


if __name__ == "__main__":
    main()

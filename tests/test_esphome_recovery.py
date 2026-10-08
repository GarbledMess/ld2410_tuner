"""Replay the package's actual YAML actions; this is not a hardware emulator."""

from pathlib import Path

import yaml

PACKAGE = Path(__file__).resolve().parents[1] / "examples/esphome/ld2410c.yaml"
SCRIPT = "ld2410_recover_parameters"
PAUSED = "ld2410_recovery_paused"
READY = "ld2410_configuration_ready"


class StopScript(Exception):
    """Stop the currently executing package script."""


class RecoveryReplay:
    """Interpret only the automation primitives used by this package, failing closed."""

    def __init__(self):
        self.package = yaml.load(
            PACKAGE.read_text().replace("${ld2410_id}", "ld2410"), Loader=yaml.BaseLoader
        )
        self.values = {
            item["id"]: item["initial_value"] == "true" for item in self.package["globals"]
        }
        self.now = 0
        self.ready = False
        self.running = False
        self.commands = []
        self.recover_on = None

    def expression(self, code):
        expressions = {
            f"return id({PAUSED});": lambda: self.values[PAUSED],
            f"return !id({PAUSED});": lambda: not self.values[PAUSED],
            "return id(ld2410_restore_engineering);": lambda: self.values[
                "ld2410_restore_engineering"
            ],
            "return id(ld2410_engineering_mode).state;": lambda: False,
            "return millis();": lambda: self.now,
            "return !id(ld2410_recovery_restarted) || uint32_t(millis() - "
            "id(ld2410_last_recovery_restart)) >= 60000U;": lambda: (
                not self.values["ld2410_recovery_restarted"]
                or self.now - self.values["ld2410_last_recovery_restart"] >= 60000
            ),
        }
        return expressions[" ".join(code.split())]()

    def condition(self, condition):
        kind, value = next(iter(condition.items()))
        if kind == "and":
            return all(self.condition(item) for item in value)
        if kind == "not":
            return not self.condition(value)
        if kind == "lambda":
            return self.expression(value)
        if kind == "script.is_running":
            assert value == SCRIPT
            return self.running
        assert kind == "binary_sensor.is_off"
        assert value == READY
        return not self.ready

    def set_ready(self):
        self.ready = True
        sensor = next(x for x in self.package["binary_sensor"] if x.get("id") == READY)
        self.actions(sensor["on_press"])

    def execute(self):
        if self.running:
            return
        self.running = True
        try:
            self.actions(self.package["script"][0]["then"])
        except StopScript:
            pass
        finally:
            self.running = False

    def actions(self, actions):
        for action in actions:
            self.action(*next(iter(action.items())))

    def action(self, kind, value):
        if kind == "if":
            branch = "then" if self.condition(value["condition"]) else "else"
            self.actions(value.get(branch, []))
        elif kind == "repeat":
            for _ in range(int(value["count"])):
                self.actions(value["then"])
        elif kind == "delay":
            self.now += int(value.removesuffix("s")) * 1000
        elif kind == "globals.set":
            raw = value["value"]
            self.values[value["id"]] = (
                raw == "true" if raw in ("true", "false") else self.expression(raw)
            )
        elif kind in ("button.press", "switch.turn_on"):
            self.commands.append(value)
            if len(self.commands) == self.recover_on:
                self.set_ready()
        elif kind == "script.execute":
            assert value == SCRIPT
            self.execute()
        elif kind == "script.stop":
            assert value == SCRIPT
            raise StopScript
        else:
            assert kind == "logger.log", f"Unsupported action: {kind}"

    def tick(self):
        self.now += 60000
        self.actions(self.package["interval"][0]["then"])

    def retry(self):
        button = next(x for x in self.package["button"] if x.get("name") == "Retry Radar Recovery")
        self.actions(button["on_press"])


def test_persistent_fault_stops_all_commands_after_one_failed_cycle():
    replay = RecoveryReplay()
    replay.execute()
    assert replay.commands.count("ld2410_radar_restart") == 1
    assert replay.commands.count("ld2410_query_params") == 6
    assert replay.values[PAUSED]
    commands = replay.commands.copy()
    for _ in range(120):
        replay.tick()
    replay.execute()  # Also guard direct script invocations.
    assert replay.commands == commands


def test_settings_return_rearms_recovery_for_another_fault_without_esp_restart():
    replay = RecoveryReplay()
    replay.execute()
    replay.set_ready()  # For example, configuration loaded through the Bluetooth app.
    assert not replay.values[PAUSED]
    commands = replay.commands.copy()
    replay.tick()
    assert replay.commands == commands
    replay.ready = False
    replay.tick()
    assert replay.commands.count("ld2410_radar_restart") == 2
    assert replay.values[PAUSED]


def test_manual_retry_is_bounded_and_respects_restart_cooldown():
    replay = RecoveryReplay()
    replay.execute()
    replay.retry()
    assert replay.commands.count("ld2410_radar_restart") == 1
    assert replay.values[PAUSED]
    replay.tick()
    replay.retry()
    assert replay.commands.count("ld2410_radar_restart") == 2
    assert replay.values[PAUSED]


def test_recovery_on_first_query_avoids_restart_and_does_not_pause():
    replay = RecoveryReplay()
    replay.recover_on = 1
    replay.execute()
    assert replay.commands == ["ld2410_query_params"]
    assert not replay.values[PAUSED]


def test_manual_retry_does_not_interrupt_active_cycle():
    replay = RecoveryReplay()
    replay.running = True
    replay.values[PAUSED] = True
    replay.retry()
    assert replay.values[PAUSED]
    assert not replay.commands

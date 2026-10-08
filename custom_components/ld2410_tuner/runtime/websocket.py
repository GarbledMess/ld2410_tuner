"""Websocket operations on the shared runtime state."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.components import websocket_api

from ..const import DOMAIN, TRAINING_STATES


def _websocket_routes():
    device = {vol.Required("device_id"): str}
    state = {vol.Required("state"): vol.In(TRAINING_STATES)}
    return [
        ("snapshot", {}, "snapshot", (), None, False),
        (
            "learn_room",
            {vol.Required("group_id"): str},
            "learn_room",
            ("group_id",),
            "invalid_request",
            False,
        ),
        (
            "apply_room",
            {
                vol.Required("group_id"): str,
                vol.Required("result_id"): str,
                vol.Required("source"): vol.In(["user", "automatic"]),
            },
            "apply_room",
            ("group_id", "result_id", "source"),
            "invalid_request",
            False,
        ),
        (
            "configure_room",
            {
                vol.Optional("group_id"): str,
                vol.Required("name"): str,
                vol.Optional("area_id"): vol.Any(str, None),
                vol.Required("device_ids"): [str],
            },
            "configure_room",
            ("group_id", "name", "area_id", "device_ids"),
            "invalid_request",
            False,
        ),
        (
            "remove_room",
            {vol.Required("group_id"): str},
            "remove_room",
            ("group_id",),
            "invalid_request",
            False,
        ),
        (
            "assess_room",
            {vol.Required("group_id"): str, vol.Optional("hours", default=24): vol.Coerce(float)},
            "assess_room",
            ("group_id", "hours"),
            "invalid_request",
            False,
        ),
        (
            "compare_results",
            {**device, vol.Optional("force", default=False): bool},
            "compare_results",
            ("device_id", "force"),
            "invalid_device",
            False,
        ),
        (
            "configure_timing",
            {vol.Required("settings"): dict},
            "configure_timing",
            ("settings",),
            "invalid_request",
            False,
        ),
        (
            "configure_presence_sources",
            {**device, vol.Required("settings"): dict},
            "configure_presence_sources",
            ("device_id", "settings"),
            "invalid_request",
            False,
        ),
        (
            "set_recording",
            {**device, vol.Required("enabled"): bool},
            "set_recording",
            ("device_id", "enabled"),
            "invalid_request",
            False,
        ),
        (
            "configure_storage",
            {vol.Required("settings"): dict},
            "configure_storage",
            ("settings",),
            "invalid_request",
            False,
        ),
        (
            "trim_storage",
            {vol.Optional("target_mib"): vol.Coerce(float)},
            "trim_storage",
            ("target_mib",),
            "invalid_request",
            False,
        ),
        (
            "set_training_state",
            {
                **device,
                **state,
                vol.Optional("timeout_seconds", default=0): vol.All(
                    vol.Coerce(int), vol.Range(min=0)
                ),
            },
            "set_training_state",
            ("device_id", "state", "timeout_seconds"),
            "invalid_device",
            True,
        ),
        ("start_learning", device, "start_learning", ("device_id",), "invalid_device", False),
        ("learn", device, "async_learn", ("device_id",), "invalid_device", False),
        (
            "apply",
            {
                **device,
                vol.Optional("slot"): vol.In(["user", "previous", "current", "automatic"]),
                vol.Optional("result_id"): str,
                vol.Optional("expected"): dict,
            },
            "apply",
            ("device_id", "slot", "result_id", "expected"),
            "invalid_device",
            False,
        ),
        (
            "configure_device_auto_apply",
            {**device, vol.Required("mode"): vol.In(["inherit", "off", "overnight", "all"])},
            "configure_device_auto_apply",
            ("device_id", "mode"),
            "invalid_request",
            False,
        ),
        (
            "configure_learning_schedule",
            {
                vol.Required("enabled"): bool,
                vol.Required("at"): str,
                vol.Optional("auto_apply"): bool,
                vol.Optional("auto_apply_scope"): vol.In(["overnight", "all"]),
            },
            "configure_learning_schedule",
            ("enabled", "at", "auto_apply", "auto_apply_scope"),
            "invalid_request",
            False,
        ),
        ("export", {vol.Optional("device_id"): str}, "export_data", ("device_id",), None, False),
        ("clear", device, "clear_samples", ("device_id",), None, True),
        (
            "label_history",
            {
                **device,
                **state,
                vol.Required("start"): vol.Coerce(float),
                vol.Required("end"): vol.Coerce(float),
            },
            "label_history_range",
            ("device_id", "start", "end", "state"),
            "invalid_history_range",
            False,
        ),
        (
            "edit_history_label",
            {
                **device,
                vol.Required("label_start"): vol.Coerce(float),
                vol.Required("label_end"): vol.Coerce(float),
                vol.Required("start"): vol.Coerce(float),
                vol.Required("end"): vol.Coerce(float),
                vol.Required("state"): vol.In(["present", "not_present", "unknown", "unlabelled"]),
                vol.Required("revision"): vol.Coerce(int),
            },
            "edit_history_label",
            ("device_id", "label_start", "label_end", "start", "end", "state", "revision"),
            "invalid_history_range",
            False,
        ),
        (
            "auto_feedback",
            {**device, vol.Required("correct"): bool},
            "record_auto_feedback",
            ("device_id", "correct"),
            "invalid_device",
            False,
        ),
        (
            "history_series_multi",
            {
                **device,
                vol.Required("keys"): [str],
                vol.Optional("hours", default=6): vol.Coerce(float),
                vol.Optional("max_points", default=400): vol.Coerce(int),
                vol.Optional("end"): vol.Coerce(float),
            },
            "async_history_series",
            ("device_id", "keys", "hours", "max_points", "end"),
            "invalid_request",
            False,
        ),
        (
            "set_gate_threshold",
            {**device, vol.Required("key"): str, vol.Required("value"): vol.Coerce(float)},
            "set_gate_threshold",
            ("device_id", "key", "value"),
            "invalid_request",
            False,
        ),
    ]


async def _websocket_result(runtime, method, fields, message):
    import inspect

    values = [message.get(field) for field in fields]
    if method == "set_training_state":
        values[-1] = values[-1] or None
    result = getattr(runtime, method)(*values)
    return await result if inspect.isawaitable(result) else result


def _websocket_handler(route):
    name, schema, method, fields, error_code, acknowledge = route

    async def handle(hass, connection, message):
        runtime = hass.data.get(DOMAIN)
        if runtime is None:
            connection.send_error(message["id"], "not_loaded", "LD2410 Tuner is not loaded")
            return
        try:
            result = await _websocket_result(runtime, method, fields, message)
        except ValueError as error:
            if error_code is None:
                raise
            connection.send_error(message["id"], error_code, str(error))
            return
        connection.send_result(message["id"], {"ok": True} if acknowledge else result)

    response = websocket_api.async_response(handle)
    command = websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/{name}", **schema})(
        response
    )
    return websocket_api.require_admin(command)


def _register_websocket_commands(hass):
    for route in _websocket_routes():
        websocket_api.async_register_command(hass, _websocket_handler(route))

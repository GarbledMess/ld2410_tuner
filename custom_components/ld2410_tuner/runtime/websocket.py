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
            "configure_learning_schedule",
            {vol.Required("enabled"): bool, vol.Required("at"): str},
            "configure_learning_schedule",
            ("enabled", "at"),
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

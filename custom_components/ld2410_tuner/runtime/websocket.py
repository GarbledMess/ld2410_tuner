"""Websocket operations on the shared runtime state."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.components import websocket_api

from ..const import DOMAIN, TRAINING_STATES
from ..history import label_counts
from ..worker.supervisor import Busy, Closed


def _websocket_routes():
    device = {vol.Required("device_id"): str}
    state = {vol.Required("state"): vol.In(TRAINING_STATES)}
    return [
        ("snapshot", {}, "snapshot", (), None, False),
        (
            "learn_set",
            {vol.Required("set_id"): str},
            "learn_set",
            ("set_id",),
            "invalid_request",
            False,
        ),
        (
            "apply_set",
            {
                vol.Required("set_id"): str,
                vol.Required("result_id"): str,
                vol.Required("source"): vol.In(["user", "automatic"]),
            },
            "apply_set",
            ("set_id", "result_id", "source"),
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
            "configure_room_sources",
            {vol.Required("group_id"): str, vol.Required("settings"): dict},
            "configure_room_sources",
            ("group_id", "settings"),
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
        (
            "configure_automatic_weight",
            {vol.Required("weight"): vol.Coerce(float)},
            "configure_automatic_weight",
            ("weight",),
            "invalid_request",
            False,
        ),
        (
            "export",
            {vol.Optional("device_id"): str},
            "async_export_data",
            ("device_id",),
            None,
            False,
        ),
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
            "label_room_range",
            {
                vol.Required("group_id"): str,
                **state,
                vol.Required("start"): vol.Coerce(float),
                vol.Required("end"): vol.Coerce(float),
            },
            "label_room_range",
            ("group_id", "start", "end", "state"),
            "invalid_request",
            False,
        ),
        (
            "edit_room_label",
            {
                vol.Required("group_id"): str,
                vol.Required("label_start"): vol.Coerce(float),
                vol.Required("label_end"): vol.Coerce(float),
                vol.Required("start"): vol.Coerce(float),
                vol.Required("end"): vol.Coerce(float),
                vol.Required("state"): vol.In(["present", "not_present", "unknown", "unlabelled"]),
                vol.Required("revision"): vol.Coerce(int),
            },
            "edit_room_label",
            ("group_id", "label_start", "label_end", "start", "end", "state", "revision"),
            "invalid_request",
            False,
        ),
        (
            "room_estimate_feedback",
            {vol.Required("group_id"): str, vol.Required("correct"): bool},
            "room_estimate_feedback",
            ("group_id", "correct"),
            "invalid_request",
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
            "refresh_references",
            {vol.Optional("device_id"): vol.Any(str, None)},
            "refresh_references",
            ("device_id",),
            "invalid_request",
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
                vol.Optional("summary"): bool,
            },
            "async_history_series",
            ("device_id", "keys", "hours", "max_points", "end", "summary"),
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
    if method in label_counts.EDITS:
        await label_counts.ensure(runtime, values[0])
    if method == "set_training_state":
        values[-1] = values[-1] or None
    result = getattr(runtime, method)(*values)
    return await result if inspect.isawaitable(result) else result


def _websocket_error(connection, message_id, error, error_code):
    if isinstance(error, Busy):
        error_code, detail = "busy", "Tuner is busy; please retry shortly"
    elif isinstance(error, Closed):
        error_code, detail = "unavailable", "Tuner workers are unavailable"
    elif error_code is None:
        raise error
    else:
        detail = str(error)
    connection.send_error(message_id, error_code, detail)


def _websocket_handler(route):
    name, schema, method, fields, error_code, acknowledge = route

    async def handle(hass, connection, message):
        runtime = hass.data.get(DOMAIN)
        if runtime is None:
            connection.send_error(message["id"], "not_loaded", "LD2410 Tuner is not loaded")
            return
        try:
            result = await _websocket_result(runtime, method, fields, message)
        except (Busy, Closed, ValueError) as error:
            _websocket_error(connection, message["id"], error, error_code)
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

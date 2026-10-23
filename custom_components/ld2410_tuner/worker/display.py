"""Home Assistant independent display calculations and bounded export validation."""

import base64
import binascii
import json
import math
import zlib

from ..const import HISTORY_KEYS
from ..history import dayfile
from . import codec

EXPORT_BYTES = 1024 * 1024
EXPORT_NODES = 32768
CHART_RAW_BYTES = 32 * 1024 * 1024
CHART_WIRE_BYTES = 1024 * 1024 - 1024
# The point fields the panel draws; the rest of a chart never leaves the worker.
POINT_FIELDS = ("t", "avg", "min", "max", "start", "end", "break_before")


def calculate(operation, view, parameters):
    """Run display jobs using the same detached day store as numerical work."""
    if operation == "charts":
        from ..presentation.charts import history_series_multi

        result = history_series_multi(view, **parameters)
        return pack_chart(result)
    if operation == "export_days":
        return recorded_days(view, parameters["device_id"])
    raise ValueError("Unsupported display operation")


def calculate_charts(views, parameters):
    """Calculate each requested radar independently inside one display child."""
    from ..presentation.charts import history_series_multi

    charts, errors = {}, {}
    for device_id in parameters["device_ids"]:
        try:
            charts[device_id] = pack_chart(
                history_series_multi(
                    views[device_id],
                    device_id=device_id,
                    keys=parameters["keys"],
                    hours=parameters["hours"],
                    max_points=parameters["max_points"],
                    end=parameters["end"],
                )
            )
        except ValueError as error:
            errors[device_id] = str(error)[:500]
    return {"charts": charts, "errors": errors}


def unpack_charts(value, device_ids):
    """Validate the room envelope before expanding any untrusted chart payloads."""
    if type(value) is not dict or set(value) != {"charts", "errors"}:
        raise ValueError("Invalid room chart envelope")
    charts, errors = value["charts"], value["errors"]
    if type(charts) is not dict or type(errors) is not dict:
        raise ValueError("Invalid room chart maps")
    _validate_chart_devices(charts, device_ids)
    _validate_chart_devices(errors, device_ids)
    if charts.keys() & errors.keys():
        raise ValueError("Conflicting room chart results")
    if any(type(error) is not str or len(error) > 500 for error in errors.values()):
        raise ValueError("Invalid room chart error")
    return {"charts": {key: unpack_chart(chart) for key, chart in charts.items()}, "errors": errors}


def _validate_chart_devices(mapping, device_ids):
    if any(type(key) is not str or key not in device_ids for key in mapping):
        raise ValueError("Invalid room chart device")


def drawn(chart):
    """``chart`` as the panel draws it: per-point geometry and the labels, nothing else."""
    if "points" in chart:
        return {**_window(chart), "points": _points(chart["points"])}
    return {
        **_window(chart),
        "series": {key: {"points": _points(s["points"])} for key, s in chart["series"].items()},
        "labels": chart["labels"],
    }


def _window(chart):
    return {name: chart[name] for name in ("start", "end", "bucket_seconds")}


def _points(points):
    return [{field: point[field] for field in POINT_FIELDS} for point in points]


def pack_chart(value):
    """Keep the drawn chart geometry inside the existing bounded result envelope."""
    value = drawn(value)
    _validate_chart(value)
    raw = json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(raw) > CHART_RAW_BYTES:
        raise ValueError("Chart exceeds expanded byte budget")
    encoded = base64.b64encode(zlib.compress(raw)).decode("ascii")
    if len(encoded) > CHART_WIRE_BYTES:
        raise ValueError("Chart exceeds transport byte budget")
    return {"chart_zlib": encoded}


def unpack_chart(value):
    """Expand chart-only data with strict byte, shape and finite-value bounds."""
    if type(value) is not dict or set(value) != {"chart_zlib"}:
        raise ValueError("Invalid chart envelope")
    encoded = value["chart_zlib"]
    if type(encoded) is not str or len(encoded) > CHART_WIRE_BYTES:
        raise ValueError("Invalid chart transport size")
    try:
        compressed = base64.b64decode(encoded, validate=True)
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, CHART_RAW_BYTES + 1)
        if len(raw) > CHART_RAW_BYTES or not decoder.eof or decoder.unused_data:
            raise ValueError("Invalid or oversized chart stream")
        # Check depth and token counts before JSON allocates the expanded tree.
        from . import protocol

        protocol._preflight(raw, 2_000_000)
        result = json.loads(raw, object_pairs_hook=protocol._pairs)
        _validate_chart(result)
        return result
    except (binascii.Error, UnicodeError, zlib.error, RecursionError) as error:
        raise ValueError("Invalid chart payload") from error


def _validate_chart(value):
    codec.validate(value, max_nodes=2_000_000, max_bytes=CHART_RAW_BYTES)
    fields = {"start", "end", "bucket_seconds", "series", "labels"}
    summary_fields = {"start", "end", "bucket_seconds", "points"}
    if type(value) is not dict or set(value) not in (fields, summary_fields):
        raise ValueError("Invalid chart fields")
    for name in ("start", "end", "bucket_seconds"):
        _number(value[name])
    if value["end"] < value["start"] or value["bucket_seconds"] <= 0:
        raise ValueError("Invalid chart window")
    if set(value) == summary_fields:
        for point in _bounded_list(value["points"], len(HISTORY_KEYS) * 1000):
            _validate_point(point)
        return
    _validate_full_chart(value)


def _validate_full_chart(value):
    series = value["series"]
    _validate_gates(series)
    for item in series.values():
        _validate_series(item)
    for label in _bounded_list(value["labels"], 8192):
        _validate_label(label)


def _validate_gates(series):
    if type(series) is not dict or not 1 <= len(series) <= len(HISTORY_KEYS):
        raise ValueError("Invalid chart series")
    if not set(series) <= set(HISTORY_KEYS):
        raise ValueError("Invalid chart gates")


def _validate_label(label):
    if type(label) is not dict or set(label) != {"start", "end", "state"}:
        raise ValueError("Invalid chart label")
    _number(label["start"])
    _number(label["end"])
    if type(label["state"]) is not str or label["end"] < label["start"]:
        raise ValueError("Invalid chart label values")


def _validate_series(item):
    if type(item) is not dict or set(item) != {"points"}:
        raise ValueError("Invalid chart series fields")
    for point in _bounded_list(item["points"], 1000):
        _validate_point(point)


def _bounded_list(values, limit):
    if type(values) is not list or len(values) > limit:
        raise ValueError("Invalid chart geometry budget")
    return values


def _validate_point(entry):
    if type(entry) is not dict or set(entry) != _POINT_FIELDS:
        raise ValueError("Invalid chart point")
    for field in POINT_FIELDS[:-1]:
        _number(entry[field])
    if type(entry["break_before"]) is not bool:
        raise ValueError("Invalid chart point break")


_POINT_FIELDS = set(POINT_FIELDS)


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("Invalid chart number")


def recorded_days(runtime, device_id):
    """Encode lossless days inside the child; reject exports exceeding its result cap."""
    if runtime._days is None:
        return {}
    found, size = {}, 0
    for date in runtime._days.days(device_id):
        day = runtime._days.day(device_id, date)
        if day is None:
            continue
        encoded = dayfile.encode_day(day)
        size += 4 * ((len(encoded) + 2) // 3) + 32
        if size > EXPORT_BYTES:
            raise ValueError("Export day data exceeds byte budget")
        found[date.isoformat()] = base64.b64encode(encoded).decode("ascii")
    return found


def check_export(value):
    """Bound source values before metadata helpers copy or summarize their contents."""
    codec.validate(value, max_nodes=EXPORT_NODES, max_bytes=EXPORT_BYTES)

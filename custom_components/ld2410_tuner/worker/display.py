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


def calculate(operation, view, parameters):
    """Run display jobs using the same detached day store as numerical work."""
    if operation == "charts":
        from ..presentation.charts import history_series_multi

        result = history_series_multi(view, **parameters)
        return pack_chart(result)
    if operation == "export_days":
        return recorded_days(view, parameters["device_id"])
    raise ValueError("Unsupported display operation")


def pack_chart(value):
    """Keep full chart geometry inside the existing bounded result envelope."""
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
        for series in result.get("series", {}).values():
            for name in ("gaps", "omitted_ranges"):
                series[name] = [tuple(span) for span in series[name]]
        return result
    except (binascii.Error, UnicodeError, zlib.error, RecursionError) as error:
        raise ValueError("Invalid chart payload") from error


def _validate_chart(value):
    codec.validate(value, max_nodes=2_000_000, max_bytes=CHART_RAW_BYTES)
    fields = {"start", "end", "bucket_seconds", "series", "labels", "basis", "modes"}
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
    if value["basis"] != "physical_span_duration" or value["modes"] not in ([], ["event"]):
        raise ValueError("Invalid chart basis")


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
    fields = {
        "sample_count",
        "observed_seconds",
        "basis",
        "count_basis",
        "points",
        "gaps",
        "gaps_truncated",
        "omitted_ranges",
    }
    if type(item) is not dict or set(item) != fields:
        raise ValueError("Invalid chart series fields")
    for name in ("sample_count", "observed_seconds"):
        _number(item[name])
    if (
        item["basis"] != "physical_span_duration"
        or item["count_basis"] != "six_second_equivalent_duration"
        or type(item["gaps_truncated"]) is not bool
    ):
        raise ValueError("Invalid chart series basis")
    for point in _bounded_list(item["points"], 1000):
        _validate_point(point)
    for name in ("gaps", "omitted_ranges"):
        for span in _bounded_list(item[name], 1000):
            _validate_range(span)


def _bounded_list(values, limit):
    if type(values) is not list or len(values) > limit:
        raise ValueError("Invalid chart geometry budget")
    return values


def _validate_point(entry):
    fields = {
        "start",
        "end",
        "min",
        "max",
        "min_start",
        "min_end",
        "max_start",
        "max_end",
        "observed_seconds",
        "avg",
        "count",
        "t",
        "break_before",
    }
    if type(entry) is not dict or set(entry) not in (fields, fields | {"anchor"}):
        raise ValueError("Invalid chart point")
    for field in fields - {"break_before"}:
        _number(entry[field])
    if type(entry["break_before"]) is not bool or (
        "anchor" in entry and entry["anchor"] is not True
    ):
        raise ValueError("Invalid chart anchor")


def _validate_range(entry):
    if type(entry) not in (tuple, list) or len(entry) != 2:
        raise ValueError("Invalid chart range")
    _number(entry[0])
    _number(entry[1])
    if entry[1] < entry[0]:
        raise ValueError("Invalid chart range endpoints")


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

"""Lossless recording pages decoded only in the numerical child."""

import datetime
import math
from bisect import bisect_right

from ..const import HISTORY_KEYS
from . import codec, protocol


def calculate(view, device_id, date, offset, limit, end, revision):
    day = view._days.day(device_id, datetime.date.fromisoformat(date), until=end)
    rows = []
    total = 0
    if day is not None:
        total = bisect_right(day.times, end)
        for index in range(offset, min(total, offset + limit)):
            values = [column[index] for column in day.columns]
            values = [
                None if value == 255 or not math.isfinite(value) else value for value in values
            ]
            rows.append([day.times[index], min(end, day.ends[index]), *values])
    after = offset + len(rows)
    return {
        "date": date,
        "keys": list(HISTORY_KEYS),
        "rows": rows,
        "total": total,
        "offset": offset,
        "next_offset": after if after < total else None,
        "end": end,
        "revision": revision,
    }


PAGE_ROWS = 10000
PAGE_RAW_BYTES = 8 * 1024 * 1024
PAGE_WIRE_BYTES = 1024 * 1024 - 1024
PAGE_NODES = 220000


def pack_page(value):
    """Compress one bounded page inside the existing one MiB worker protocol."""
    import base64
    import json
    import zlib

    _validate_page(value)
    while True:
        raw = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
        if len(raw) > PAGE_RAW_BYTES:
            raise ValueError("Recording page exceeds expanded byte budget")
        encoded = base64.b64encode(zlib.compress(raw)).decode("ascii")
        if len(encoded) <= PAGE_WIRE_BYTES:
            return {"recording_zlib": encoded}
        if len(value["rows"]) <= 1:
            raise ValueError("Recording page exceeds transport byte budget")
        rows = value["rows"][: len(value["rows"]) // 2]
        value = {**value, "rows": rows, "next_offset": value["offset"] + len(rows)}


def unpack_page(value):
    """Bound compressed bytes, expansion, JSON allocation and exact page shape."""
    import base64
    import binascii
    import json
    import zlib

    if type(value) is not dict or set(value) != {"recording_zlib"}:
        raise ValueError("Invalid recording page envelope")
    encoded = value["recording_zlib"]
    if type(encoded) is not str or len(encoded) > PAGE_WIRE_BYTES:
        raise ValueError("Invalid recording page transport size")
    try:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(base64.b64decode(encoded, validate=True), PAGE_RAW_BYTES + 1)
        if len(raw) > PAGE_RAW_BYTES or not decoder.eof or decoder.unused_data:
            raise ValueError("Invalid or oversized recording page stream")
        protocol._preflight(raw, PAGE_NODES)
        result = json.loads(raw, object_pairs_hook=protocol._pairs)
        _validate_page(result)
        return result
    except (binascii.Error, UnicodeError, zlib.error, RecursionError) as error:
        raise ValueError("Invalid recording page payload") from error


def _validate_page(value):
    codec.validate_schema(
        value, _validate_page_shape, max_nodes=PAGE_NODES, max_bytes=PAGE_RAW_BYTES
    )


def _validate_page_shape(value, budget):
    fields = {"date", "keys", "rows", "total", "offset", "next_offset", "end", "revision"}
    if type(value) is not dict or set(value) != fields or value["keys"] != list(HISTORY_KEYS):
        raise ValueError("Invalid recording page fields")
    _page_budget(value, budget)
    date = datetime.date.fromisoformat(value["date"])
    if date.isoformat() != value["date"]:
        raise ValueError("Invalid recording date")
    _validate_page_metadata(value)
    rows = value["rows"]
    if type(rows) is not list or len(rows) > PAGE_ROWS:
        raise ValueError("Invalid recording row budget")
    budget.add(21 * len(rows))  # Each row is one list plus twenty scalar values.
    for row in rows:
        _validate_row(row, value["end"])


def _page_budget(value, budget):
    budget.keys(value)
    if type(value["keys"]) is not list:
        raise ValueError("Invalid recording page fields")
    # Eight fields plus the root; the keys list adds its eighteen strings.
    budget.add(17 + len(HISTORY_KEYS))
    for key, item in value.items():
        budget.string(key)
        if key not in ("keys", "rows"):
            budget.string(item)
    for key in value["keys"]:
        budget.string(key)


def _validate_page_metadata(value):
    for field in ("total", "offset", "revision"):
        if type(value[field]) is not int or not 0 <= value[field] < 2**63:
            raise ValueError("Invalid recording page metadata")
    after = value["next_offset"]
    if after is not None and (
        type(after) is not int or not value["offset"] < after < value["total"]
    ):
        raise ValueError("Invalid recording next offset")
    if not codec.schema_number(value["end"]):
        raise ValueError("Invalid recording cutoff")


def _validate_row(row, end):
    if type(row) is not list or len(row) != 20:
        raise ValueError("Invalid recording row shape")
    if any(not codec.schema_number(value) for value in row[:2]):
        raise ValueError("Invalid recording row times")
    if not row[0] <= row[1] <= end:
        raise ValueError("Invalid recording row interval")
    if any(value is not None and (not codec.schema_number(value)) for value in row[2:]):
        raise ValueError("Invalid recording gate value")

"""Synthetic boundary cases pin errors before schema validation is fused."""

import base64
import copy
import json
import zlib

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.const import HISTORY_KEYS
from tuner_under_test.worker import display, recordings


def chart():
    return {
        "start": 0,
        "end": 1,
        "bucket_seconds": 1,
        "points": [
            {"t": 0, "avg": 1, "min": 1, "max": 1, "start": 0, "end": 1, "break_before": False}
        ],
    }


def page():
    return {
        "date": "1970-01-01",
        "keys": list(HISTORY_KEYS),
        "rows": [[0, 1, *([None] * 18)]],
        "total": 1,
        "offset": 0,
        "next_offset": None,
        "end": 1,
        "revision": 0,
    }


def wire(value, kind):
    raw = value if type(value) is bytes else json.dumps(value).encode()
    return {kind: base64.b64encode(zlib.compress(raw)).decode()}


@pytest.mark.parametrize(
    "module,factory,field", [(display, chart, "start"), (recordings, page, "end")]
)
@pytest.mark.parametrize(
    "bad,message",
    [
        (2**63, "Metadata integer exceeds 64-bit range"),
        (float("inf"), "Nonfinite metadata number"),
        (float("nan"), "Nonfinite metadata number"),
        (object(), "Unsupported metadata value"),
        ([[[[[[[[[[[[[[[[[[[[[0]]]]]]]]]]]]]]]]]]]]], "Value node or depth budget exceeded"),
    ],
)
def test_scalar_rejections(module, factory, field, bad, message):
    value = factory()
    value[field] = bad
    validate = module._validate_chart if module is display else module._validate_page
    with pytest.raises(ValueError, match=message):
        validate(value)


@pytest.mark.parametrize(
    "module,factory,kind", [(display, chart, "chart_zlib"), (recordings, page, "recording_zlib")]
)
@pytest.mark.parametrize(
    "raw,message",
    [
        (b'{"x":1,"x":2}', "Duplicate metadata key"),
        (b"[" * 26 + b"0" + b"]" * 26, "Metadata node or depth budget exceeded"),
        (b'{"x":"unterminated}', "Unterminated metadata string"),
    ],
)
def test_json_preflight_rejections(module, factory, kind, raw, message):
    unpack = module.unpack_chart if module is display else module.unpack_page
    with pytest.raises(ValueError, match=message):
        unpack(wire(raw, kind))


@pytest.mark.parametrize("name", ["PAGE_NODES", "PAGE_RAW_BYTES"])
def test_recording_aggregate_budgets(monkeypatch, name):
    monkeypatch.setattr(recordings, name, 1)
    with pytest.raises(ValueError, match="budget"):
        recordings._validate_page(page())


@pytest.mark.parametrize(
    "module,factory,kind,prefix",
    [(display, chart, "chart_zlib", "CHART"), (recordings, page, "recording_zlib", "PAGE")],
)
@pytest.mark.parametrize("failure", ["transport", "expanded", "trailing", "truncated"])
def test_transport_limits(monkeypatch, module, factory, kind, prefix, failure):
    unpack = module.unpack_chart if module is display else module.unpack_page
    value = wire(factory(), kind)
    if failure == "transport":
        monkeypatch.setattr(module, prefix + "_WIRE_BYTES", 1)
    elif failure == "expanded":
        monkeypatch.setattr(module, prefix + "_RAW_BYTES", 1)
    else:
        compressed = base64.b64decode(value[kind])
        compressed = compressed + b"x" if failure == "trailing" else compressed[:-1]
        value[kind] = base64.b64encode(compressed).decode()
    with pytest.raises(ValueError):
        unpack(value)


@pytest.mark.parametrize(
    "field,bad,message",
    [
        ("points", [None] * 18001, "Invalid chart geometry budget"),
        ("points", [None], "Invalid chart point"),
        ("start", True, "Invalid chart number"),
        ("end", -1, "Invalid chart window"),
    ],
)
def test_chart_shape_errors(field, bad, message):
    value = chart()
    value[field] = bad
    with pytest.raises(ValueError, match=message):
        display._validate_chart(value)


@pytest.mark.parametrize(
    "field,bad,message",
    [
        ("rows", [[0] * 19], "Invalid recording row shape"),
        ("rows", [[0, 2, *([None] * 18)]], "Invalid recording row interval"),
        ("rows", [[False, 1, *([None] * 18)]], "Invalid recording row times"),
        ("rows", [[0, 1, True, *([None] * 17)]], "Invalid recording gate value"),
        ("rows", [None] * 10001, "Invalid recording row budget"),
        ("offset", True, "Invalid recording page metadata"),
    ],
)
def test_page_shape_errors(field, bad, message):
    value = page()
    value[field] = bad
    with pytest.raises(ValueError, match=message):
        recordings._validate_page(value)


def test_chart_string_budgets(monkeypatch):
    value = {
        "start": 0,
        "end": 1,
        "bucket_seconds": 1,
        "series": {"g0_move": {"points": []}},
        "labels": [{"start": 0, "end": 1, "state": "é" * 40}],
    }
    monkeypatch.setattr(display, "CHART_RAW_BYTES", 79)
    with pytest.raises(ValueError, match="Metadata string exceeds byte budget"):
        display._validate_chart(value)
    value["labels"][0]["state"] = "é" * 20
    value["labels"] *= 2
    with pytest.raises(ValueError, match="Aggregate value string budget exceeded"):
        display._validate_chart(value)


@pytest.mark.parametrize(
    "factory,pack,unpack",
    [
        (chart, display.pack_chart, display.unpack_chart),
        (page, recordings.pack_page, recordings.unpack_page),
    ],
)
def test_roundtrip_exact(factory, pack, unpack):
    value = factory()
    original = copy.deepcopy(value)
    assert unpack(pack(value)) == original
    assert value == original


@pytest.mark.parametrize("limit", [55, 56, 57])
def test_page_exact_node_boundary(monkeypatch, limit):
    monkeypatch.setattr(recordings, "PAGE_NODES", limit)
    if limit < 56:
        with pytest.raises(ValueError, match="Value node budget exceeded"):
            recordings._validate_page(page())
    else:
        recordings._validate_page(page())


@pytest.mark.parametrize("factory,module", [(chart, display), (page, recordings)])
@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_exact_string_boundary(monkeypatch, factory, module, delta):
    value = factory()

    def string_bytes(item):
        if type(item) is dict:
            return sum(string_bytes(k) + string_bytes(v) for k, v in item.items())
        if type(item) is list:
            return sum(map(string_bytes, item))
        return len(item.encode("utf-8")) if type(item) is str else 0

    size = string_bytes(value)
    monkeypatch.setattr(
        module, "CHART_RAW_BYTES" if module is display else "PAGE_RAW_BYTES", size + delta
    )
    validate = module._validate_chart if module is display else module._validate_page
    if delta < 0:
        with pytest.raises(ValueError, match="Aggregate value string budget exceeded"):
            validate(value)
    else:
        validate(value)


@pytest.mark.parametrize("bad", [[], {}, [1], None, 1, False])
def test_label_type_error_stays_schema_error(bad):
    value = {
        "start": 0,
        "end": 1,
        "bucket_seconds": 1,
        "series": {"g0_move": {"points": []}},
        "labels": [{"start": 0, "end": 1, "state": bad}],
    }
    with pytest.raises(ValueError, match="Invalid chart label values"):
        display._validate_chart(value)


@pytest.mark.parametrize(
    "field,bad,exception,message",
    [
        ("date", [], TypeError, "fromisoformat: argument must be str"),
        ("date", "19700101", ValueError, "Invalid recording date"),
        ("revision", -1, ValueError, "Invalid recording page metadata"),
        ("next_offset", 1, ValueError, "Invalid recording next offset"),
        ("keys", [], ValueError, "Invalid recording page fields"),
    ],
)
def test_page_metadata_error_order(field, bad, exception, message):
    value = page()
    value[field] = bad
    with pytest.raises(exception, match=message):
        recordings._validate_page(value)


@pytest.mark.parametrize(
    "module,factory,field", [(display, chart, "points"), (recordings, page, "rows")]
)
def test_generic_fault_wins_over_shape_fault(module, factory, field):
    value = factory()
    value[field] = []
    value["unexpected"] = float("inf")
    validate = module._validate_chart if module is display else module._validate_page
    with pytest.raises(ValueError, match="Nonfinite metadata number"):
        validate(value)


@pytest.mark.parametrize(
    "count,module,kind",
    [(2_000_000, display, "chart_zlib"), (220000, recordings, "recording_zlib")],
)
def test_preflight_token_limit(count, module, kind):
    unpack = module.unpack_chart if module is display else module.unpack_page
    raw = b"[" + b"0," * count + b"0]"
    with pytest.raises(ValueError, match="Metadata node or depth budget exceeded"):
        unpack(wire(raw, kind))


def test_chart_generic_node_limit():
    with pytest.raises(ValueError, match="Value node budget exceeded"):
        display._validate_chart([None] * 2_000_000)


@pytest.mark.parametrize(
    "field,bad,message",
    [
        ("labels", [None] * 8193, "Invalid chart geometry budget"),
        ("series", {"g0_move": {"points": [None] * 1001}}, "Invalid chart geometry budget"),
        ("series", {}, "Invalid chart series"),
        ("series", {"alien": {"points": []}}, "Invalid chart gates"),
    ],
)
def test_full_chart_geometry_limits(field, bad, message):
    value = {
        "start": 0,
        "end": 1,
        "bucket_seconds": 1,
        "series": {"g0_move": {"points": []}},
        "labels": [],
    }
    value[field] = bad
    with pytest.raises(ValueError, match=message):
        display._validate_chart(value)

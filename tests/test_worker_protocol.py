"""The worker boundary uses capped, versioned JSON, never executable objects."""

import json

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.worker import protocol


def test_roundtrip():
    payload = {"device": "radar", "labels": [[1, 2, "present"]], "weight": 0.5}
    assert protocol.unpack(protocol.pack("learn", payload), "learn") == payload


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b'{"version":true,"kind":"learn","payload":{}}',
        b'{"version":2,"kind":"learn","payload":{}}',
        b'{"version":1,"kind":"wrong","payload":{}}',
        b'{"version":1,"kind":"learn","payload":[]}',
        b'{"version":1,"kind":"learn","payload":{"x":NaN}}',
        b'{"version":1,"kind":"learn","payload":{},"extra":0}',
        b'{"version":1,"version":1,"kind":"learn","payload":{}}',
        b"\xff",
        b"[[[",
    ],
)
def test_invalid_envelopes(raw):
    with pytest.raises(protocol.ProtocolRejected):
        protocol.unpack(raw, "learn")


def test_bytes_checked_before_parse(monkeypatch):
    monkeypatch.setattr(json, "loads", lambda *args, **kwargs: pytest.fail("parsed oversized data"))
    with pytest.raises(protocol.ProtocolRejected):
        protocol.unpack(b"x" * 100, "learn", max_bytes=10)


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), {1: "wrong key"}, object(), 2**100, (1, 2)]
)
def test_unsupported_values(value):
    with pytest.raises(protocol.ProtocolRejected):
        protocol.pack("learn", {"value": value})


def test_deep_tree_rejected_before_serializing(monkeypatch):
    value = []
    for _ in range(30):
        value = [value]
    monkeypatch.setattr(json, "dumps", lambda *args, **kwargs: pytest.fail("serialized deep tree"))
    with pytest.raises(protocol.ProtocolRejected):
        protocol.pack("learn", {"value": value})


def test_node_limit():
    with pytest.raises(protocol.ProtocolRejected):
        protocol.pack("learn", {"values": [0] * 100}, max_nodes=10)


def test_string_byte_limit():
    with pytest.raises(protocol.ProtocolRejected):
        protocol.pack("learn", {"value": "x" * 100}, max_bytes=50)


def test_cycles_are_bounded():
    payload = {}
    payload["self"] = payload
    with pytest.raises(protocol.ProtocolRejected):
        protocol.pack("learn", payload)


def test_aggregate_strings_rejected_before_serializing(monkeypatch):
    monkeypatch.setattr(
        json, "dumps", lambda *args, **kwargs: pytest.fail("serialized oversized tree")
    )
    with pytest.raises(protocol.ProtocolRejected):
        protocol.pack("learn", {"values": ["x" * 40] * 5}, max_bytes=150)


@pytest.mark.parametrize("payload", [{"value": "\ud800"}, {"\ud800": 1}])
def test_surrogates_are_protocol_errors(payload):
    with pytest.raises(protocol.ProtocolRejected):
        protocol.pack("learn", payload)


@pytest.mark.parametrize("raw", [b"[" + b"0," * 1000 + b"0]", b"[" * 30 + b"0" + b"]" * 30])
def test_tree_budget_checked_before_json_parse(raw, monkeypatch):
    monkeypatch.setattr(json, "loads", lambda *args, **kwargs: pytest.fail("parsed excessive tree"))
    with pytest.raises(protocol.ProtocolRejected):
        protocol.unpack(raw, "learn", max_nodes=10)


def test_structural_characters_inside_strings_are_not_tokens():
    payload = {"value": '[,]}{\\"true false null'}
    assert protocol.unpack(protocol.pack("learn", payload), "learn") == payload


@pytest.mark.parametrize(
    "payload", [{"n": -1.25e10, "truth": True, "nil": None}, {"value": '"\\\t\n⛄'}]
)
def test_scalar_and_escape_roundtrip(payload):
    assert protocol.unpack(protocol.pack("learn", payload), "learn") == payload


@pytest.mark.parametrize("option", ["max_bytes", "max_nodes"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, 2**30])
def test_invalid_protocol_bounds(option, value):
    with pytest.raises(ValueError):
        protocol.pack("learn", {}, **{option: value})


@pytest.mark.parametrize("kind,payload", [("wrong", {}), ("learn", [])])
def test_invalid_outgoing_envelope(kind, payload):
    with pytest.raises(protocol.ProtocolRejected):
        protocol.pack(kind, payload)


def test_exact_serialized_size_is_checked():
    with pytest.raises(protocol.ProtocolRejected):
        protocol.pack("learn", {}, max_bytes=40)


def test_deep_raw_checked_before_parse(monkeypatch):
    monkeypatch.setattr(json, "loads", lambda *args, **kwargs: pytest.fail("parsed deep tree"))
    with pytest.raises(protocol.ProtocolRejected):
        protocol.unpack(b"[" * 30 + b"0" + b"]" * 30, "learn")


def test_unterminated_escaped_quotes_rejected_before_parse(monkeypatch):
    monkeypatch.setattr(
        json, "loads", lambda *args, **kwargs: pytest.fail("parsed unterminated string")
    )
    with pytest.raises(protocol.ProtocolRejected):
        protocol.unpack(b'"' + b'\\"' * 10000, "learn")


@pytest.mark.parametrize(
    "encoding",
    ["utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be", "utf-8-sig"],
)
def test_alternate_encodings_cannot_bypass_scan(encoding, monkeypatch):
    raw = json.dumps(
        {"version": 1, "kind": "learn", "payload": {"x": '"', "y": [0] * 1000, "z": '"'}}
    ).encode(encoding)
    monkeypatch.setattr(
        json, "loads", lambda *args, **kwargs: pytest.fail("parsed unsupported encoding")
    )
    with pytest.raises(protocol.ProtocolRejected):
        protocol.unpack(raw, "learn", max_nodes=40)


class _ByteScanner:
    """The original byte-at-a-time preflight, kept as the oracle for the fast scan."""

    def __init__(self):
        self.depth = 0
        self.quoted = self.escaped = self.primitive = False

    def node(self, byte):
        if self.quoted:
            if self.escaped:
                self.escaped = False
            elif byte == 92:
                self.escaped = True
            elif byte == 34:
                self.quoted = False
            return False
        if byte in b" \t\r\n,:]}":
            self.primitive = False
            if byte in b"]}":
                self.depth -= 1
            return False
        if self.primitive and byte not in b'"{[':
            return False
        self.quoted = byte == 34
        self.primitive = not self.quoted and byte not in b"{["
        return True


def _byte_preflight(raw, max_nodes):
    scanner, nodes = _ByteScanner(), 0
    for byte in raw:
        if not scanner.node(byte):
            continue
        nodes += 1
        if nodes > max_nodes or scanner.depth > 24:
            return False
        if byte in b"{[":
            scanner.depth += 1
    return not scanner.quoted


def _accepted(raw, max_nodes):
    try:
        protocol._preflight(raw, max_nodes)
    except protocol.ProtocolRejected:
        return False
    return True


def _fuzz_cases():
    import random

    pieces = [b"[", b"]", b"{", b"}", b",", b":", b" ", b"\n", b"\t", b'"', b"\\", b"0"]
    pieces += [b"tru", b"e", b"-1.5", b"x", "⛄".encode()]
    for seed in range(400):
        rng = random.Random(seed)
        weights = [rng.random() for _ in pieces]
        raw = b"".join(rng.choices(pieces, weights=weights, k=rng.randrange(0, 160)))
        if seed % 3 == 0:
            depth = rng.randrange(20, 30)
            raw = b"[" * depth + raw + b"]" * depth
        yield raw
    for depth in range(22, 28):
        for inner in (b"", b"0", b'""', b"[]", b"{}"):
            yield b"[" * depth + inner + b"]" * depth
            yield b"{" * depth + inner + b"}" * depth


def test_preflight_accepts_exactly_what_the_byte_scanner_accepts():
    for raw in _fuzz_cases():
        for max_nodes in (1, 3, 10, 40, 100000):
            assert _accepted(raw, max_nodes) == _byte_preflight(raw, max_nodes), (raw, max_nodes)


def _preflight_calls(rows):
    import sys

    raw = json.dumps({"v": [{"t": i, "s": 'a"b', "x": [1.5, True, None]} for i in range(rows)]})
    calls = [0]

    def count(frame, event, arg):
        calls[0] += event == "call"

    sys.setprofile(count)
    try:
        protocol._preflight(raw.encode(), 10**7)
    finally:
        sys.setprofile(None)
    return calls[0]


def test_preflight_work_does_not_grow_per_byte():
    # Worker replies reach megabytes; the scan must not run Python per byte.
    assert _preflight_calls(4000) < 1.2 * _preflight_calls(1000)

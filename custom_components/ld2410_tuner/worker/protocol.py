"""Versioned, allocation-bounded metadata for disposable numerical workers."""

import json
import math

VERSION = 1


class ProtocolRejected(ValueError):
    """Input cannot cross the worker boundary safely."""


def _limits(max_bytes, max_nodes):
    for limit in (max_bytes, max_nodes):
        if type(limit) is not int or not 0 < limit <= 16 * 1024 * 1024:
            raise ValueError("Protocol bounds must be positive bounded integers")


def _scalar(value, max_bytes):
    kind = type(value)
    if kind is str:
        if len(value) > max_bytes or len(value.encode("utf-8")) > max_bytes:
            raise ProtocolRejected("Metadata string exceeds byte budget")
        return len(value.encode("utf-8"))
    elif kind is int:
        if not -(2**63) <= value < 2**63:
            raise ProtocolRejected("Metadata integer exceeds 64-bit range")
    elif kind is float:
        if not math.isfinite(value):
            raise ProtocolRejected("Nonfinite metadata number")
    elif kind not in (bool, type(None)):
        raise ProtocolRejected("Unsupported metadata value")
    return 0


def _check(value, max_bytes, max_nodes):
    pending, nodes, size = [(value, 0)], 0, 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > max_nodes or depth > 24:
            raise ProtocolRejected("Metadata node or depth budget exceeded")
        if type(item) not in (dict, list):
            size += _scalar(item, max_bytes)
            if size > max_bytes:
                raise ProtocolRejected("Aggregate metadata string budget exceeded")
            continue
        pending.extend(_children(item, depth, max_nodes - nodes - len(pending)))


def _children(item, depth, remaining):
    count = len(item) * (2 if type(item) is dict else 1)
    if count > remaining:
        raise ProtocolRejected("Metadata node budget exceeded")
    if type(item) is dict:
        if any(type(key) is not str for key in item):
            raise ProtocolRejected("Metadata keys must be strings")
        yield from ((key, depth + 1) for key in item)
        yield from ((child, depth + 1) for child in item.values())
    else:
        yield from ((child, depth + 1) for child in item)


def pack(kind, payload, *, max_bytes=1024 * 1024, max_nodes=32768):
    """Validate the tree before serialization, then enforce exact UTF-8 size."""
    _limits(max_bytes, max_nodes)
    if type(payload) is not dict or kind not in ("learn", "result"):
        raise ProtocolRejected("Invalid worker message")
    envelope = {"version": VERSION, "kind": kind, "payload": payload}
    try:
        _check(envelope, max_bytes, max_nodes)
    except UnicodeError as error:
        raise ProtocolRejected("Invalid metadata Unicode") from error
    raw = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode(
        "utf-8"
    )
    if len(raw) > max_bytes:
        raise ProtocolRejected("Worker message exceeds byte budget")
    return raw


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolRejected("Duplicate metadata key")
        result[key] = value
    return result


def unpack(raw, kind, *, max_bytes=1024 * 1024, max_nodes=32768):
    """Cap bytes before parsing; reject ambiguous versions and malformed trees."""
    _limits(max_bytes, max_nodes)
    if type(raw) is not bytes or len(raw) > max_bytes:
        raise ProtocolRejected("Invalid or oversized worker message")
    try:
        text = _utf8(raw)
        _preflight(raw, max_nodes)
        envelope = json.loads(text, object_pairs_hook=_pairs)
        _check(envelope, max_bytes, max_nodes)
        if type(envelope) is not dict or set(envelope) != {"version", "kind", "payload"}:
            raise ProtocolRejected("Invalid worker envelope")
        if type(envelope["version"]) is not int or envelope["version"] != VERSION:
            raise ProtocolRejected("Unsupported worker protocol version")
        if (
            envelope["kind"] != kind
            or kind not in ("learn", "result")
            or type(envelope["payload"]) is not dict
        ):
            raise ProtocolRejected("Unexpected worker message")
        return envelope["payload"]
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ProtocolRejected("Invalid worker metadata") from error


class _Scanner:
    """Linear byte scan; JSON syntax is still validated by the standard parser."""

    def __init__(self):
        self.depth = 0
        self.quoted = self.escaped = self.primitive = False

    def _string(self, byte):
        if self.escaped:
            self.escaped = False
        elif byte == 92:
            self.escaped = True
        elif byte == 34:
            self.quoted = False

    def node(self, byte):
        if self.quoted:
            self._string(byte)
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


def _preflight(raw, max_nodes):
    scanner, nodes = _Scanner(), 0
    for byte in raw:
        if not scanner.node(byte):
            continue
        nodes += 1
        if nodes > max_nodes or scanner.depth > 24:
            raise ProtocolRejected("Metadata node or depth budget exceeded")
        if byte in b"{[":
            scanner.depth += 1
    if scanner.quoted:
        raise ProtocolRejected("Unterminated metadata string")


def _utf8(raw):
    text = raw.decode("utf-8")
    if "\x00" in text or text.startswith("\ufeff"):
        raise ProtocolRejected("Worker metadata must be UTF-8 without a BOM or literal NUL")
    return text

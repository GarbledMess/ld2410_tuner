"""Versioned, allocation-bounded metadata for disposable numerical workers."""

import json
import math
import re
from itertools import accumulate, compress, repeat
from operator import eq, sub

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


# Strings go first, so structural bytes inside them are not tokens. Each string then
# becomes one quote and each run of primitive bytes one "n"; openers "(", closers ")".
_STRING = re.compile(rb'"[^"\\]*(?:\\.[^"\\]*)*"', re.DOTALL)
_PRIMITIVE = re.compile(rb'[^ \t\r\n,:\[\]{}"]+')
_TOKENS = bytes.maketrans(b'"[{]}', b"n(())")
_DELTAS = bytes.maketrans(b"(n)", b"\x02\x01\x00")


def _preflight(raw, max_nodes):
    """Linear scan with C-level regex and byte passes; the standard parser checks syntax."""
    tokens, strings = _STRING.subn(b'"', raw)
    if tokens.count(b'"') != strings:
        raise ProtocolRejected("Unterminated metadata string")
    tokens = _PRIMITIVE.sub(b"n", tokens).translate(_TOKENS, b" \t\r\n,:")
    if tokens.count(b"n") + tokens.count(b"(") > max_nodes:
        raise ProtocolRejected("Metadata node or depth budget exceeded")
    # Depth after each token: a leaf starts at that depth, an opener one level above it.
    depths = list(accumulate(map(sub, tokens.translate(_DELTAS), repeat(1))))
    leaf = max(compress(depths, map(eq, tokens, repeat(ord("n")))), default=0)
    opener = max(compress(depths, map(eq, tokens, repeat(ord("(")))), default=1) - 1
    if max(leaf, opener) > 24:
        raise ProtocolRejected("Metadata node or depth budget exceeded")


def _utf8(raw):
    text = raw.decode("utf-8")
    if "\x00" in text or text.startswith("\ufeff"):
        raise ProtocolRejected("Worker metadata must be UTF-8 without a BOM or literal NUL")
    return text

"""Closed, bounded value codec: no pickle, imports, or executable type names."""

from . import protocol

TAG = "$worker"


def encode(value, *, max_nodes=32768):
    """Copy portable values only after a bounded traversal of the source tree."""
    validate(value, max_nodes=max_nodes)
    return _encode(value)


def validate(value, *, max_nodes=32768, max_bytes=1024 * 1024):
    """Check a source tree without allocating its encoded copy."""
    pending = [(value, 0)]
    nodes = size = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > max_nodes or depth > 20:
            raise ValueError("Value node or depth budget exceeded")
        if type(item) in (dict, list, tuple):
            children = _children(item, max_nodes - nodes - len(pending))
            pending.extend((child, depth + 1) for child in children)
        else:
            size += protocol._scalar(item, max_bytes)
            if size > max_bytes:
                raise ValueError("Aggregate value string budget exceeded")


def _children(item, remaining):
    count = len(item) * (2 if type(item) is dict else 1)
    if count > remaining:
        raise ValueError("Value node budget exceeded")
    return (*item.keys(), *item.values()) if type(item) is dict else item


def _encode(value):
    if type(value) is tuple:
        return {TAG: "tuple", "items": [_encode(item) for item in value]}
    if type(value) is list:
        return [_encode(item) for item in value]
    if type(value) is dict:
        if all(type(key) is str for key in value) and TAG not in value:
            return {key: _encode(item) for key, item in value.items()}
        if any(type(key) not in (str, int, float) for key in value):
            raise ValueError("Unsupported mapping key")
        return {TAG: "mapping", "items": [[key, _encode(item)] for key, item in value.items()]}
    return value


def decode(value):
    """Restore only tuples and scalar-key mappings from validated JSON."""
    if type(value) is list:
        return [decode(item) for item in value]
    if type(value) is not dict:
        return value
    if TAG not in value:
        return {key: decode(item) for key, item in value.items()}
    if set(value) != {TAG, "items"} or type(value["items"]) is not list:
        raise ValueError("Invalid value tag")
    if value[TAG] == "tuple":
        return tuple(decode(item) for item in value["items"])
    if value[TAG] != "mapping":
        raise ValueError("Unknown value tag")
    return _mapping(value["items"])


def _mapping(items):
    result = {}
    for pair in items:
        if type(pair) is not list or len(pair) != 2 or type(pair[0]) not in (str, int, float):
            raise ValueError("Invalid mapping entry")
        if pair[0] in result:
            raise ValueError("Duplicate restored mapping key")
        result[pair[0]] = decode(pair[1])
    return result


def pack_result(value, **limits):
    """Serialize a numerical result with the strict versioned protocol."""
    return protocol.pack("result", {"value": encode(value)}, **limits)


def unpack_result(raw, **limits):
    payload = protocol.unpack(raw, "result", **limits)
    if set(payload) != {"value"}:
        raise ValueError("Invalid result payload")
    return decode(payload["value"])

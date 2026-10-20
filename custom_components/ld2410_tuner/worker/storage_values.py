"""Bounded JSON-native normalization for HA's persisted metadata values."""

import json

from . import codec

MAX_BYTES = 16 * 1024 * 1024
MAX_NODES = 1024 * 1024


def _key(value):
    if type(value) is str:
        return value
    if value is None or type(value) in (bool, int, float):
        return json.dumps(value, allow_nan=False)
    raise ValueError("Unsupported storage mapping key")


def validate(value, *, max_nodes=MAX_NODES, max_bytes=MAX_BYTES):
    """Bound source traversal before copies and refuse lossy mapping conversion."""
    codec.validate(value, max_nodes=max_nodes, max_bytes=max_bytes)
    pending = [value]
    while pending:
        item = pending.pop()
        if type(item) is dict:
            keys = {_key(key) for key in item}
            if len(keys) != len(item):
                raise ValueError("Storage mapping key collision")
            pending.extend(item.values())
        elif type(item) in (list, tuple):
            pending.extend(item)


def normalize(value, *, max_nodes=MAX_NODES, max_bytes=MAX_BYTES):
    """Preserve HA JSON semantics without changing the generic worker protocol."""
    validate(value, max_nodes=max_nodes, max_bytes=max_bytes)
    return _normalize(value)


def _normalize(value):
    if type(value) is dict:
        return {_key(key): _normalize(item) for key, item in value.items()}
    if type(value) in (list, tuple):
        return [_normalize(item) for item in value]
    return value

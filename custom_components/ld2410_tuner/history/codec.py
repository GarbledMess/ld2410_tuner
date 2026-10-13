"""The compact payload of stored history: byte columns, delta-coded, under zlib, as base64."""

from __future__ import annotations

import base64
import zlib
from itertools import accumulate
from operator import sub

SPAN = 65535  # A sample's largest offset from its block start, and a block's default length.
VERSION = 4  # Written by this version: a 2-byte offset and 18 energies per sample.
# Bytes per sample of each block version. Versions 2 and 3 also hold the radar's guess (a code
# and a confidence), which guesses.py now keeps as runs; they are read for migration only.
STRIDES = {1: 20, 2: 22, 3: 22, 4: 20}
DELTA_CODED = (3, 4)  # Versions 1 and 2 are plain zlib.
ROW = STRIDES[VERSION]

_MOD = (256).__rmod__  # x -> x % 256
_LOW = (255).__and__  # x -> x % 256 for x >= 0


def encode_payload(raw, stride=ROW):
    """Delta-code each byte column before zlib; values and timestamps are lossless.
    Column by column in C (``map``), not byte by byte in Python: blocks are encoded on every
    flush and by every cleanup."""
    columns = bytearray()
    for column in range(stride):
        values = raw[column::stride]
        columns += bytes(map(_MOD, map(sub, values, b"\x00" + values[:-1])))
    return base64.b64encode(zlib.compress(bytes(columns), 6)).decode("ascii")


def _inflate(encoded, stride, count):
    raw = zlib.decompress(base64.b64decode(encoded, validate=True))
    if count < 0 or len(raw) != count * stride:
        raise ValueError("Invalid history block length or version")
    return raw


def decode_column_list(encoded, stride, count):
    """The ``stride`` byte columns (a byte for each of the ``count`` rows) of a payload."""
    raw = _inflate(encoded, stride, count)
    # Undo the column deltas with running sums (accumulate): no per-byte Python loop.
    return [
        bytes(map(_LOW, accumulate(raw[column * count : (column + 1) * count])))
        for column in range(stride)
    ]


def decode_columns(encoded, stride, count):
    """The ``count`` rows of ``stride`` bytes that ``encode_payload`` made from ``encoded``,
    the columns interleaved back into rows by extended-slice assignment."""
    restored = bytearray(count * stride)
    for column, values in enumerate(decode_column_list(encoded, stride, count)):
        restored[column::stride] = values
    return bytes(restored)


def decode_payload(encoded, version, count):
    if version not in STRIDES:
        raise ValueError("Invalid history block length or version")
    if version in DELTA_CODED:
        return decode_columns(encoded, STRIDES[version], count)
    return _inflate(encoded, STRIDES[version], count)

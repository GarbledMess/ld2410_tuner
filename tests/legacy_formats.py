"""Builders of the JSON history formats earlier releases stored, for conversion tests only.

Nothing in the integration writes these any more; ``history/convert.py`` reads them once.
Synthetic data only.
"""

import base64
import struct
import zlib

STRIDES = {1: 20, 2: 22, 3: 22, 4: 20}
RUN = struct.Struct(">ddBBB")
CODES = {"present": 1, "not_present": 2}


def _columns(raw, stride):
    """Each byte column delta-coded (versions 3 and 4)."""
    out = bytearray()
    for column in range(stride):
        previous = 0
        for value in raw[column::stride]:
            out.append((value - previous) % 256)
            previous = value
    return bytes(out)


def _encode(raw, stride, delta):
    return base64.b64encode(zlib.compress(_columns(raw, stride) if delta else raw, 6)).decode()


def sampled_block(start, rows, version=4):
    """A stored sampled block. ``rows`` are ``(offset, energies)`` or, for versions 2 and 3,
    ``(offset, energies, code, percent)`` with the radar's guess in each row."""
    stride = STRIDES[version]
    raw = b"".join(
        struct.pack(">H", row[0]) + bytes(row[1]) + (bytes(row[2:4]) if stride == 22 else b"")
        for row in rows
    )
    return {
        "version": version,
        "start": start,
        "end": start + rows[-1][0],
        "count": len(rows),
        "data": _encode(raw, stride, version in (3, 4)),
    }


def samples(start, values, step=6, version=4):
    """One version-``version`` block of rows ``step`` seconds apart; ``values`` are g0_move
    readings (the other gates unread)."""
    rows = [(index * step, [value] + [255] * 17) for index, value in enumerate(values)]
    return sampled_block(start, rows, version)


def guess_chunks(runs):
    """Stored guess run chunks of ``runs`` ``(start, end, state, level, basis)``."""
    if not runs:
        return []
    raw = b"".join(
        RUN.pack(start, end, CODES[state], level, basis) for start, end, state, level, basis in runs
    )
    return [
        {
            "version": 1,
            "start": runs[0][0],
            "end": max(run[1] for run in runs),
            "count": len(runs),
            "data": _encode(raw, RUN.size, True),
        }
    ]


def blocks(samples, span=65535):
    """Version-4 blocks holding ``(timestamp, energies)`` samples in time order (energies: 18
    bytes; anything after them is ignored), up to 60 rows and ``span`` seconds a block, as the
    old recorder grouped them."""
    found, group = [], []
    for timestamp, row in samples:
        if group and (
            len(group) >= 60
            or timestamp - group[0][0] > span
            or abs(timestamp - group[0][0] - round(timestamp - group[0][0])) > 1e-6
        ):
            found.append(_group_block(group))
            group = []
        group.append((timestamp, bytes(row[:18])))
    if group:
        found.append(_group_block(group))
    return found


def _group_block(group):
    start = group[0][0]
    rows = [(round(timestamp - start), list(row)) for timestamp, row in group]
    return sampled_block(start, rows)

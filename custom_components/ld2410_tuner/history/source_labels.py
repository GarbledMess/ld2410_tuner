"""Confirm buffered external presence without changing energies or human labels."""

import struct
import zlib

from ..const import HISTORY_KEYS
from ..presence import reference_training
from .cleanup import decode_payload, encode_payload

WIDTH = len(HISTORY_KEYS)


def confirm_presence(runtime, device_id, start, end, confidence):
    if end <= start:
        return
    device = runtime.data["devices"][device_id]
    samples = runtime._history_runtime.get(device_id, {}).get("samples", [])
    confirmed = []
    changed = _confirm_pending(samples, start, end, confidence, confirmed)
    changed += _confirm_history(device.get("history", []), start, end, confidence, confirmed)
    if changed:
        for timestamp, row in sorted(confirmed):
            reference_training.observe(runtime, device, timestamp, row, external=True)
        observations = device.setdefault("auto", {}).setdefault("observations", {})
        observations["present"] = observations.get("present", 0) + changed
        runtime._history_cache.clear()
        runtime._schedule_save()


def _confirm_pending(samples, start, end, confidence, confirmed=None):
    changed = 0
    for index, (timestamp, row) in enumerate(samples):
        if start <= timestamp < end and len(row) == WIDTH + 2 and row[WIDTH] == 0:
            samples[index] = (timestamp, row[:WIDTH] + bytes((1, confidence)))
            if confirmed is not None:
                confirmed.append(samples[index])
            changed += 1
    return changed


def _confirm_history(history, start, end, confidence, confirmed):
    changed = 0
    for index in range(len(history) - 1, -1, -1):
        block = history[index]
        if block.get("end", block["start"] + 65535) < start:
            break
        if block["start"] >= end or block.get("version", 1) == 1:
            continue
        updated, count = _confirm_block(block, start, end, confidence, confirmed)
        if count:
            history[index] = updated
            changed += count
    return changed


def _confirm_block(block, start, end, confidence, confirmed=None):
    try:
        raw = bytearray(decode_payload(block["data"], block["version"], block["count"]))
    except (KeyError, TypeError, ValueError, zlib.error):
        return block, 0  # Leave damaged blocks to the normal history cleanup.

    stride, changed = WIDTH + 4, 0
    for offset in range(0, len(raw), stride):
        timestamp = block["start"] + struct.unpack_from(">H", raw, offset)[0]
        code = offset + 2 + WIDTH
        if start <= timestamp < end and raw[code] == 0:
            raw[code : code + 2] = bytes((1, confidence))
            if confirmed is not None:
                confirmed.append((timestamp, bytes(raw[offset + 2 : offset + stride])))
            changed += 1
    if not changed:
        return block, 0
    return {**block, "version": 3, "data": encode_payload(bytes(raw))}, changed

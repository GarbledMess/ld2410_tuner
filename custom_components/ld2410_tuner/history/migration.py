"""One-time conversion of a store written before guess runs (storage minor version 2).

Until then every history row held the radar's guess (a code and a confidence). Rows are now
energies only and the guesses are runs (``guesses``). ``convert_guesses`` is pure and
idempotent: it works on a copy, so a conversion interrupted before the store is saved leaves
the old file intact and is simply run again, and an already converted store is returned as it
is. It reads every old row, so it runs in the executor.
"""

from __future__ import annotations

import math
import time

from .cleanup import clean_history
from .codec import VERSION


def _old_layout(device):
    blocks = device.get("history") if isinstance(device, dict) else None
    return isinstance(blocks, list) and any(
        isinstance(block, dict)
        and isinstance(block.get("version", 1), int)
        and block.get("version", 1) < VERSION
        for block in blocks
    )


def _convert(device):
    if not _old_layout(device):
        return device
    # The normal cleanup does the conversion, keeping every retained row and expiring none.
    updated, _stats = clean_history(device, [], time.time(), math.inf)
    return {**device, "history": updated["history"], "guess_runs": updated["guess_runs"]}


def convert_guesses(data):
    """``data`` with every device's old-layout history rewritten as version 4 blocks and guess
    runs; devices already in the new layout, and ``data`` without devices, are unchanged."""
    devices = data.get("devices")
    if not isinstance(devices, dict):
        return data
    return {**data, "devices": {key: _convert(device) for key, device in devices.items()}}

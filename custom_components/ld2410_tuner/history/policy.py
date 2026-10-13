"""Recording and age-retention policy shared by storage, learning and charts."""

import math

from .codec import SPAN

DAY = 86400
STORED = ("history", "guess_runs")  # A device's stored blocks and guess run chunks, by time.
MIB = 1024 * 1024
DEFAULTS = {
    "thin_after_days": 7,
    "minimum_confidence": 50,
    "automatic_days": 30,
    "human_days": 30,
    "max_mib": 100,
}


def settings(data):
    return {**DEFAULTS, **data.get("storage_settings", {})}


def validate(values):
    if set(values) != set(DEFAULTS):
        raise ValueError("Supply all storage settings")
    for value in values.values():
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("Storage settings must be finite numbers")
    start, automatic, human = (
        values[key] for key in ("thin_after_days", "automatic_days", "human_days")
    )
    if not 0 <= start < automatic <= human <= 3650:
        raise ValueError("Use thinning age < automatic expiry <= human expiry (maximum 3650 days)")
    if not 0 <= values["minimum_confidence"] <= 100 or not 1 <= values["max_mib"] <= 10240:
        raise ValueError("Confidence must be 0–100%; storage limit must be 1–10240 MiB")
    return dict(values)


def keep_automatic(timestamp, confidence, now, policy):
    """Whether an automatic sample ``timestamp`` whose guess had ``confidence`` percent
    (0: none) survives thinning."""
    age = (now - timestamp) / DAY
    start, end = policy["thin_after_days"], policy["automatic_days"]
    if age < start:
        return True
    if age >= end:
        return False
    minimum = policy["minimum_confidence"]
    cutoff = minimum + (100 - minimum) * (age - start) / (end - start)
    return confidence >= cutoff


def retention_seconds(data):
    return settings(data)["human_days"] * DAY


def retained(blocks, cutoff):
    """The stored ``blocks`` (history blocks or guess run chunks) that end at or after
    ``cutoff``: retention, in one place for everything stored by time."""
    return [b for b in blocks if float(b.get("end", float(b.get("start", 0)) + SPAN)) >= cutoff]

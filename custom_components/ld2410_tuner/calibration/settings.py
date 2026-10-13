"""Integration-wide "Automatic evidence weight", stored beside the learning schedule.

Learning jobs read it once through ``automatic_weight`` and pass it down; pure fitting code
takes it as a parameter. Each automatic observation counts this weight x its confidence of a
human one (``scoring.automatic_share``).
"""

import math

KEY = "automatic_weight"
# Chosen by the held-out blended sweep (eval/auto-weight-blend-report.md).
DEFAULT_AUTOMATIC_WEIGHT = 0.0


def automatic_weight(data):
    value = data.get("learning_schedule", {}).get(KEY)
    return DEFAULT_AUTOMATIC_WEIGHT if value is None else value


def signature(data):
    """Learn-signature entry, empty at the default so earlier signatures stay identical."""
    value = automatic_weight(data)
    return {} if value == DEFAULT_AUTOMATIC_WEIGHT else {KEY: value}


def summary(data):
    return {KEY: automatic_weight(data), "default_automatic_weight": DEFAULT_AUTOMATIC_WEIGHT}


def configure(runtime, value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("Automatic evidence weight must be a number from 0 to 1")
    runtime.data.setdefault("learning_schedule", {})[KEY] = float(value)
    runtime._schedule_save()
    return summary(runtime.data)

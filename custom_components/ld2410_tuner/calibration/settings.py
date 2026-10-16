"""Integration-wide "Automatic evidence weight", stored beside the learning schedule.

Learning jobs read it once through ``automatic_weight`` and pass it down; pure fitting code
takes it as a ceiling. Episode support sets the blend share, and learner reliability corrects
automatic confidence without changing the raw admission floor.
"""

import math

from .constants import METHOD
from .quality import QUALITY_VERSION

KEY = "automatic_weight"
# Current checkpoint; positive activation requires the adaptive held-out acceptance experiment.
DEFAULT_AUTOMATIC_WEIGHT = 0.0


def automatic_weight(data):
    value = data.get("learning_schedule", {}).get(KEY)
    return DEFAULT_AUTOMATIC_WEIGHT if value is None else value


def signature(data):
    """Explicit policy identity; older unversioned learning results are incompatible."""
    return {
        KEY: automatic_weight(data),
        "quality_version": QUALITY_VERSION,
        "learner_method": METHOD,
    }


def summary(data):
    return {KEY: automatic_weight(data), "default_automatic_weight": DEFAULT_AUTOMATIC_WEIGHT}


def configure(runtime, value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("Automatic evidence weight must be a number from 0 to 1")
    runtime.data.setdefault("learning_schedule", {})[KEY] = float(value)
    runtime._schedule_save()
    return summary(runtime.data)

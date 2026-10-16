"""Legal integer gate thresholds from standard Home Assistant number metadata."""

import math
from decimal import Decimal
from functools import lru_cache

DEFAULT = {"min": 0, "max": 100, "step": 1}


def from_state(state):
    attributes = getattr(state, "attributes", {}) or {}
    domain = {key: attributes.get(key, value) for key, value in DEFAULT.items()}
    allowed(domain)
    return {key: float(value) for key, value in domain.items()}


def allowed(domain=None):
    domain = DEFAULT if domain is None else domain
    low, high, step = (domain.get(key, value) for key, value in DEFAULT.items())
    if any(
        type(value) not in (int, float) or not math.isfinite(value) for value in (low, high, step)
    ):
        raise ValueError("Gate min, max and step must be finite numbers")
    if not 0 <= low <= high <= 100 or step <= 0:
        raise ValueError("Gate range must be inside 0–100 with a positive step")
    return _allowed(low, high, step)


@lru_cache(maxsize=256)
def _allowed(low, high, step):
    origin, stride = Decimal(str(low)), Decimal(str(step))
    values = tuple(
        value
        for value in range(math.ceil(low), math.floor(high) + 1)
        if (Decimal(value) - origin) % stride == 0
    )
    if not values:
        raise ValueError("Advertised gate range has no usable integer thresholds")
    return values


def normalize(value, domain=None):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("Threshold must be a finite number")
    return min(allowed(domain), key=lambda candidate: (abs(candidate - value), -candidate))


def permitted(values, domains):
    return set(values) == set(domains) and all(
        type(value) in (int, float) and math.isfinite(value) and value in allowed(domains[key])
        for key, value in values.items()
    )


def read(runtime, entities):
    states = getattr(getattr(runtime, "hass", None), "states", None)
    return {
        key: from_state(states.get(entity_id) if states else None)
        for key, entity_id in entities.items()
    }

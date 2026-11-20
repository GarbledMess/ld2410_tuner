"""Legal integer gate thresholds from standard Home Assistant number metadata.

Thresholds here are always the radar's own: a gate triggers on energy above it. A radar
running the package's Gate Rounding shows and accepts rounded values instead. A control at
T with step s holds the radar threshold ``ceil(T - s/2) - 1`` (0 for T = 0), so every raw
energy that reports T or more passes. Such a domain is marked ``rounded``; conversion
happens only where a control is read or written.
"""

import math
from decimal import Decimal
from functools import lru_cache

DEFAULT = {"min": 0, "max": 100, "step": 1}


ROUNDING_SUFFIX = "_gate_rounding"


def from_state(state, rounded=False):
    attributes = getattr(state, "attributes", {}) or {}
    domain = {key: attributes.get(key, value) for key, value in DEFAULT.items()}
    allowed(domain)
    domain = {key: float(value) for key, value in domain.items()}
    return {**domain, "rounded": True} if rounded else domain


def rounded_device(entities):
    """The package's Gate Rounding control is among this device's registry entries."""
    return any(
        entry.domain == "number" and entry.entity_id.endswith(ROUNDING_SUFFIX) for entry in entities
    )


def rounded_entity(runtime, entity_id, registry=None):
    from homeassistant.helpers import entity_registry as er

    from ..entity_index import read as index

    registry = er.async_get(runtime.hass) if registry is None else registry
    entry = registry.async_get(entity_id)
    device = getattr(entry, "device_id", None)
    return bool(device) and rounded_device(index(runtime, registry).by_device.get(device, ()))


def radar_value(control, domain=None):
    """The radar threshold a control value holds."""
    if not (domain or {}).get("rounded"):
        return control
    return 0 if control <= 0 else max(0, math.ceil(control - domain["step"] / 2) - 1)


def control_value(radar, domain=None):
    """The control value to write for a radar threshold; the largest is the one shown."""
    if not (domain or {}).get("rounded"):
        return radar
    found = _controls(*_key(domain)).get(radar)
    if found is None:
        raise ValueError(f"Threshold {radar:g} is not reachable through this gate's rounding")
    return found


def reading(state, rounded=False):
    """The radar threshold an HA control state reports; NaN when it has no number."""
    try:
        value = float(state.state) if state else math.nan
    except (TypeError, ValueError):
        return math.nan
    if not rounded or not math.isfinite(value):
        return value
    return radar_value(value, from_state(state, rounded=True))


def bounds(domain=None):
    """The lowest and highest threshold a manual value may name."""
    domain = DEFAULT if domain is None else domain
    if domain.get("rounded"):
        values = allowed(domain)
        return values[0], values[-1]
    return domain["min"], domain["max"]


def _key(domain):
    return domain["min"], domain["max"], domain["step"]


def allowed(domain=None):
    domain = DEFAULT if domain is None else domain
    low, high, step = (domain.get(key, value) for key, value in DEFAULT.items())
    if any(
        type(value) not in (int, float) or not math.isfinite(value) for value in (low, high, step)
    ):
        raise ValueError("Gate min, max and step must be finite numbers")
    if not 0 <= low <= high <= 100 or step <= 0:
        raise ValueError("Gate range must be inside 0–100 with a positive step")
    if domain.get("rounded"):
        return tuple(_controls(low, high, step))
    return _allowed(low, high, step)


@lru_cache(maxsize=256)
def _controls(low, high, step):
    """``{radar threshold: control value}``, in rising order, for a rounded control."""
    found = {}
    for control in _allowed(low, high, step):
        found[radar_value(control, {"step": step, "rounded": True})] = control
    return dict(sorted(found.items()))


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


def read(runtime, entities, *, state_get=None):
    states = getattr(getattr(runtime, "hass", None), "states", None)
    state_get = (states.get if states else lambda _: None) if state_get is None else state_get
    rounded = _rounded_reader(runtime) if states else lambda _: False
    return {
        key: from_state(state_get(entity_id), rounded(entity_id))
        for key, entity_id in entities.items()
    }


def _rounded_reader(runtime):
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(runtime.hass)
    return lambda entity_id: rounded_entity(runtime, entity_id, registry)

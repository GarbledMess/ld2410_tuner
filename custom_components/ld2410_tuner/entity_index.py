"""The entity registry, read once for the questions a poll asks of it."""

from __future__ import annotations

from typing import NamedTuple

from .const import GATE_RE

COMBINED_SUFFIX = "gate_energies"


class Entities(NamedTuple):
    """What the entity registry held when it was read."""

    registry: object
    size: int
    by_device: dict  # device id -> its entities, in registry order.
    # device id -> {"entities": {energy entity id: {"gate", "kind"}}, "combined": the
    # radar's Gate Energies sensor (runtime/gate_report.py) or None}.
    radars: dict
    timing: dict  # device id -> its timing entities by field (calibration/timing_config.py).


def read(runtime, registry) -> Entities:
    """The registry's entities by device and the radars among them. A poll asks the registry
    many questions about a handful of devices; the registry (thousands of entities in a busy
    home) is read once, and again when its size changes or it announces a change (``forget``,
    from ``runtime.discovery.handle_registry_update``). Home Assistant announces every registry
    change; a change of the same size that was somehow not announced stays unseen until the
    next event."""
    known = getattr(runtime, "_entity_index", None)
    if known is None or known.registry is not registry or known.size != len(registry.entities):
        known = runtime._entity_index = _scan(registry)
    return known


def _scan(registry) -> Entities:
    by_device, radars, combined = {}, {}, {}
    for entity in registry.entities.values():
        if not entity.device_id:
            continue
        by_device.setdefault(entity.device_id, []).append(entity)
        if energy := _energy(entity):
            radar = radars.setdefault(entity.device_id, {"entities": {}})
            radar["entities"][entity.entity_id] = energy
        elif named(entity, "sensor", COMBINED_SUFFIX):
            combined.setdefault(entity.device_id, []).append(entity.entity_id)
    for device_id, radar in radars.items():
        found = combined.get(device_id, ())
        radar["combined"] = found[0] if len(found) == 1 else None
    return Entities(registry, len(registry.entities), by_device, radars, {})


def named(entry, domain, suffix) -> bool:
    """Whether a registry ``entry`` is an enabled ``domain`` entity whose id or original name
    ends with ``suffix``, as the ESPHome package names it."""
    if entry.domain != domain or getattr(entry, "disabled_by", None):
        return False
    name = str(getattr(entry, "original_name", "") or "").lower().replace(" ", "_")
    return entry.entity_id.endswith(suffix) or name.endswith(suffix)


def _energy(entity):
    """``{"gate", "kind"}`` of a radar's gate energy sensor, None for any other entity."""
    match = entity.domain == "sensor" and GATE_RE.match(entity.entity_id.split(".", 1)[1])
    if match and match.group("metric") == "energy":
        return {"gate": int(match.group("gate")), "kind": match.group("kind")}
    return None


def forget(runtime) -> None:
    """The registry changed: read it again when it is next asked."""
    runtime._entity_index = None

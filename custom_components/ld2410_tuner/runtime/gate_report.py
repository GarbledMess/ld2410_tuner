"""Combined Gate Report: a radar's 18 gate energies in one Gate Energies state.

The ESPHome package's Combined Gate Report switch (off by default) makes the radar publish
every gate change in one text sensor and refresh its 18 gate sensors only slowly, so Home
Assistant handles one state change per radar frame instead of one per gate. The tuner keeps the
switch on for every radar it includes and reads the combined sensor wherever it carries a frame;
anything else (an older package, the switch off, the radar offline) leaves the gate sensors in
charge, as before.
"""

from __future__ import annotations

import logging

from homeassistant.helpers import entity_registry as er

from ..calibration import device_io, recovery
from ..const import HISTORY_KEYS
from ..entity_index import named

_LOGGER = logging.getLogger(__name__)
SWITCH_SUFFIX = "combined_gate_report"


def parse(state):
    """``{gate key: reading or None}`` of a frame like ``"10,0,,20,…"`` (18 fields, an empty
    one unknown), or None when ``state`` is not a frame."""
    fields = state.split(",") if isinstance(state, str) else ()
    if len(fields) != len(HISTORY_KEYS):
        return None
    frame = {}
    for key, field in zip(HISTORY_KEYS, fields, strict=True):
        value = _reading(field)
        if value is False:
            return None
        frame[key] = value
    return frame


def _reading(field):
    """A field's reading, None when unknown, False when it is not a reading."""
    if not field:
        return None
    try:
        value = float(field)
    except ValueError:
        return False
    return value if 0 <= value <= 100 else False  # NaN fails both comparisons.


async def ensure_on(runtime):
    """Turn the switch on for each included radar that has it off; a failure waits for the
    next check."""
    for device_id, device in list(runtime.data["devices"].items()):
        if not device.get("entities") or device.get("excluded"):
            continue
        entity_id = recovery.capability(runtime, device_id, "switch", (SWITCH_SUFFIX,))
        if recovery._switch_state(runtime, entity_id) != "off":
            continue
        try:
            await device_io._call(runtime, "switch", "turn_on", {"entity_id": entity_id})
        except ValueError as error:
            _LOGGER.warning("Could not turn on %s; retrying later: %s", entity_id, error)


async def turn_off_all(hass):
    """Return every radar to individual gate reports (the integration is being removed)."""
    for entry in list(er.async_get(hass).entities.values()):
        state = hass.states.get(entry.entity_id)
        if not named(entry, "switch", SWITCH_SUFFIX) or not state or state.state != "on":
            continue
        try:
            await hass.services.async_call(
                "switch", "turn_off", {"entity_id": entry.entity_id}, blocking=True
            )
        except Exception as error:  # Removal goes on for the other radars.
            _LOGGER.warning("Could not turn off %s: %s", entry.entity_id, error)

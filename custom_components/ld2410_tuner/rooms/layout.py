"""Organise radars into areas, zones and leaves; every snapshot radar is placed."""

EMPTY_NOTE = "No radars in this group"
REMOVED_NOTE = "Area removed"


def build(areas, members, groups, device_ids, recording, device_areas):
    """Return the layout tree for the radars in ``device_ids``.

    ``areas`` maps Home Assistant area id to name. ``members`` is the radar inventory
    (device id to ``name``/``area_id``/``recording_enabled``) and supplies display
    names for ordering. ``groups`` maps group id to ``name``, ``area_id``, ``kind``
    (``area`` or ``zone``) and ``device_ids``. ``device_ids`` is the snapshot's radar
    universe: only these are placed and each is placed at least once. ``recording``
    maps device id to whether recording is enabled, and ``device_areas`` maps device id
    to its Home Assistant area id (or ``None``); both decide where a radar that is in
    no group falls back to. The optional ``note`` and ``not_recording`` keys are present
    only when non-empty.
    """
    known = set(device_ids)
    order = _sorter(members)
    zones, loose = _split_groups(areas, groups)
    zone_ids = {gid: _known(group, known) for gid, group in {**_flat(zones), **loose}.items()}
    area_group = {
        group["area_id"]: gid
        for gid, group in groups.items()
        if group["kind"] == "area" and group["area_id"] in areas
    }
    rest = {
        area_id: _remainder(groups[gid], known, zones.get(area_id, {}), zone_ids)
        for area_id, gid in area_group.items()
    }
    placed = {d for ids in zone_ids.values() for d in ids} | {
        d for ids in rest.values() for d in ids
    }
    fallback = _fallback(sorted(known - placed), device_areas, areas)
    plan = _Plan(areas, groups, zones, zone_ids, area_group, rest, fallback, recording, order)
    nodes = _area_nodes(plan, known, device_areas)
    nodes += [
        _group_node(plan, gid, group) for gid, group in sorted(loose.items(), key=plan.group_key)
    ]
    if fallback.get(None):
        nodes.append(
            _node(
                "unassigned",
                "unassigned",
                "Unassigned",
                None,
                fallback[None],
                plan,
                fallback=fallback[None],
            )
        )
    return nodes


class _Plan:
    """Everything the node builders need, computed once by ``build``."""

    def __init__(
        self, areas, groups, zones, zone_ids, area_group, rest, fallback, recording, order
    ):
        self.areas = areas
        self.groups = groups
        self.zones = zones
        self.zone_ids = zone_ids
        self.area_group = area_group
        self.rest = rest
        self.fallback = fallback
        self.recording = recording
        self.order = order

    def group_key(self, item):
        return item[1]["name"].casefold(), item[0]


def _sorter(members):
    def key(device_id):
        return str(members.get(device_id, {}).get("name") or device_id).casefold(), device_id

    return key


def _split_groups(areas, groups):
    """Zones keyed by existing area, and top-level groups (area-less or area removed)."""
    zones, loose = {}, {}
    for gid, group in groups.items():
        if group["kind"] == "area":
            continue
        if group.get("area_id") in areas:
            zones.setdefault(group["area_id"], {})[gid] = group
        else:
            loose[gid] = group
    return zones, loose


def _flat(zones):
    return {gid: group for by_area in zones.values() for gid, group in by_area.items()}


def _known(group, known):
    return sorted({d for d in group["device_ids"] if d in known})


def _remainder(area_group, known, zones, zone_ids):
    """Area-group members that sit in none of the area's zones."""
    covered = {d for gid in zones for d in zone_ids[gid]}
    return [d for d in _known(area_group, known) if d not in covered]


def _fallback(unplaced, device_areas, areas):
    """Group unplaced radars under their Home Assistant area, else ``None``."""
    result = {}
    for device_id in unplaced:
        area_id = device_areas.get(device_id)
        result.setdefault(area_id if area_id in areas else None, []).append(device_id)
    return result


def _node(node_id, kind, name, group_id, devices, plan, children=(), note=None, fallback=()):
    node = {
        "id": node_id,
        "kind": kind,
        "name": name,
        "group_id": group_id,
        "devices": sorted(devices, key=plan.order),
        "children": list(children),
    }
    paused = [d for d in node["devices"] if d in fallback and not plan.recording.get(d, True)]
    if paused:
        node["not_recording"] = paused
    if not note and group_id and not devices and not children:
        note = EMPTY_NOTE
    if note:
        node["note"] = note
    return node


def _group_node(plan, gid, group):
    removed = group.get("area_id") is not None
    return _node(
        gid,
        "group",
        group["name"],
        gid,
        plan.zone_ids[gid],
        plan,
        note=REMOVED_NOTE if removed else None,
    )


def _area_nodes(plan, known, device_areas):
    wanted = set(plan.area_group) | set(plan.zones)
    wanted |= {a for d, a in device_areas.items() if d in known and a in plan.areas}
    ids = sorted(wanted, key=lambda a: (plan.areas[a].casefold(), a))
    return [_area_node(plan, area_id) for area_id in ids]


def _area_node(plan, area_id):
    name = plan.areas[area_id]
    gid = plan.area_group.get(area_id)
    fallback = plan.fallback.get(area_id, [])
    remainder = plan.rest.get(area_id, []) + fallback
    zones = plan.zones.get(area_id, {})
    if not zones:
        return _node(f"area:{area_id}", "area", name, gid, remainder, plan, fallback=fallback)
    children = [
        _node(zid, "zone", zone["name"], zid, plan.zone_ids[zid], plan)
        for zid, zone in sorted(zones.items(), key=plan.group_key)
    ]
    if remainder:
        children.append(
            _node(
                f"area:{area_id}/rest",
                "rest",
                f"Elsewhere in {name}",
                None,
                remainder,
                plan,
                fallback=fallback,
            )
        )
    return _node(f"area:{area_id}", "area", name, gid, [], plan, children)

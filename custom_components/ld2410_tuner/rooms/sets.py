"""Learning sets: multi-radar groups linked by shared radars learn together."""


def _eligible(groups):
    zoned_areas = {item["area_id"] for item in groups.values() if item["kind"] == "zone"}
    return {
        key: item
        for key, item in groups.items()
        if len(item["device_ids"]) > 1
        and not (item["kind"] == "area" and item["area_id"] in zoned_areas)
    }


def learning_sets(groups):
    """Return {set_id: {"group_ids", "device_ids"}} from the given group definitions."""
    parent = {}

    def find(radar):
        while parent[radar] != radar:
            parent[radar] = parent[parent[radar]]
            radar = parent[radar]
        return radar

    eligible = _eligible(groups)
    for item in eligible.values():
        first, *rest = item["device_ids"]
        parent.setdefault(first, first)
        for radar in rest:
            parent.setdefault(radar, radar)
            parent[find(radar)] = find(first)
    members = {}
    for key, item in eligible.items():
        members.setdefault(find(item["device_ids"][0]), []).append(key)
    found = {}
    for group_ids in members.values():
        ordered = sorted(group_ids)
        devices = {radar for key in ordered for radar in eligible[key]["device_ids"]}
        found["+".join(ordered)] = {"group_ids": ordered, "device_ids": sorted(devices)}
    return dict(sorted(found.items()))


def set_for_device(found, device_id):
    for set_id, item in found.items():
        if device_id in item["device_ids"]:
            return set_id
    return None

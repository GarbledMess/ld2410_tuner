"""The room-first layout tree places every snapshot radar at least once."""

import unittest

import test_tuner as harness  # noqa: F401  (loads the package under test)
from tuner_under_test.rooms import layout

AREAS = {"office": "Office", "lounge": "Lounge"}


def area_group(area_id, ids):
    return {
        "name": AREAS.get(area_id, area_id),
        "area_id": area_id,
        "kind": "area",
        "device_ids": ids,
        "automatic": True,
    }


def zone(name, area_id, ids):
    return {"name": name, "area_id": area_id, "kind": "zone", "device_ids": ids, "automatic": False}


def run(areas, groups, device_areas, recording=None, device_ids=None, names=None):
    ids = sorted(device_areas) if device_ids is None else device_ids
    members = {
        key: {
            "name": (names or {}).get(key, key),
            "area_id": device_areas[key],
            "recording_enabled": True,
        }
        for key in device_areas
    }
    return layout.build(
        areas, members, groups, ids, recording or dict.fromkeys(ids, True), device_areas
    )


def node(node_id, kind, name, group_id, devices=(), children=(), **extra):
    return {
        "id": node_id,
        "kind": kind,
        "name": name,
        "group_id": group_id,
        "devices": list(devices),
        "children": list(children),
        **extra,
    }


def placed(nodes):
    found = []
    for item in nodes:
        found.extend(item["devices"])
        found.extend(placed(item["children"]))
    return found


class LayoutTests(unittest.TestCase):
    def test_no_areas_all_unassigned(self):
        result = run({}, {}, {"b": None, "a": None})
        self.assertEqual(result, [node("unassigned", "unassigned", "Unassigned", None, ["a", "b"])])

    def test_no_devices_no_nodes(self):
        self.assertEqual(run({}, {}, {}), [])

    def test_area_without_zones_places_directly(self):
        groups = {"area:office": area_group("office", ["a", "b"])}
        result = run(AREAS, groups, {"a": "office", "b": "office"})
        self.assertEqual(result, [node("area:office", "area", "Office", "area:office", ["a", "b"])])

    def test_area_with_zones_and_rest_leaf(self):
        groups = {
            "area:office": area_group("office", ["a", "b", "c"]),
            "zone:1": zone("Desk", "office", ["a", "b"]),
        }
        result = run(AREAS, groups, dict.fromkeys("abc", "office"))
        leaves = [
            node("zone:1", "zone", "Desk", "zone:1", ["a", "b"]),
            node("area:office/rest", "rest", "Elsewhere in Office", None, ["c"]),
        ]
        self.assertEqual(result, [node("area:office", "area", "Office", "area:office", (), leaves)])

    def test_zones_cover_area_means_no_rest_leaf(self):
        groups = {
            "area:office": area_group("office", ["a"]),
            "zone:1": zone("Desk", "office", ["a"]),
        }
        result = run(AREAS, groups, {"a": "office"})
        self.assertEqual([c["kind"] for c in result[0]["children"]], ["zone"])

    def test_overlapping_zones_repeat_radar(self):
        groups = {
            "area:office": area_group("office", ["a", "b"]),
            "zone:1": zone("Desk", "office", ["a", "b"]),
            "zone:2": zone("Door", "office", ["b"]),
        }
        result = run(AREAS, groups, {"a": "office", "b": "office"})
        self.assertEqual([c["devices"] for c in result[0]["children"]], [["a", "b"], ["b"]])

    def test_override_pulls_radar_from_other_area(self):
        groups = {
            "area:office": area_group("office", ["a"]),
            "area:lounge": area_group("lounge", ["b"]),
        }
        groups = {
            "area:office": area_group("office", ["a", "b"]),
            "area:lounge": area_group("lounge", ["b"]),
        }
        result = run(AREAS, groups, {"a": "office", "b": "lounge"})
        self.assertEqual(
            [(n["id"], n["devices"]) for n in result],
            [("area:lounge", ["b"]), ("area:office", ["a", "b"])],
        )

    def test_area_less_group_top_level(self):
        groups = {"zone:9": zone("Open-plan", None, ["a", "b"])}
        result = run(AREAS, groups, {"a": None, "b": "office", "c": None})
        self.assertEqual(
            result,
            [
                node("area:office", "area", "Office", None),
                node("zone:9", "group", "Open-plan", "zone:9", ["a", "b"]),
                node("unassigned", "unassigned", "Unassigned", None, ["c"]),
            ],
        )

    def test_deleted_area_zone_becomes_group_with_note(self):
        groups = {"zone:9": zone("Desk", "gone", ["a"])}
        result = run({}, groups, {"a": None})
        self.assertEqual(
            result, [node("zone:9", "group", "Desk", "zone:9", ["a"], note="Area removed")]
        )

    def test_recording_disabled_falls_back_marked_not_recording(self):
        groups = {"area:office": area_group("office", ["a"])}
        result = run(AREAS, groups, {"a": "office", "b": "office"}, {"a": True, "b": False})
        self.assertEqual(
            result,
            [node("area:office", "area", "Office", "area:office", ["a", "b"], not_recording=["b"])],
        )

    def test_not_recording_without_area_goes_unassigned_marked(self):
        result = run(AREAS, {}, {"a": None}, {"a": False})
        self.assertEqual(
            result,
            [node("unassigned", "unassigned", "Unassigned", None, ["a"], not_recording=["a"])],
        )

    def test_not_recording_in_area_with_zones_lands_in_rest_leaf(self):
        groups = {
            "area:office": area_group("office", ["a"]),
            "zone:1": zone("Desk", "office", ["a"]),
        }
        result = run(AREAS, groups, {"a": "office", "b": "office"}, {"a": True, "b": False})
        rest = result[0]["children"][1]
        self.assertEqual(
            (rest["kind"], rest["devices"], rest["not_recording"]), ("rest", ["b"], ["b"])
        )

    def test_empty_group_keeps_header_with_note(self):
        groups = {"zone:9": zone("Open-plan", None, [])}
        result = run(AREAS, groups, {})
        self.assertEqual(
            result,
            [node("zone:9", "group", "Open-plan", "zone:9", note="No radars in this group")],
        )

    def test_empty_zone_in_area_keeps_note(self):
        groups = {"zone:1": zone("Desk", "office", ["ghost"])}
        result = run(AREAS, groups, {})
        leaf = result[0]["children"][0]
        self.assertEqual((leaf["devices"], leaf["note"]), ([], "No radars in this group"))

    def test_empty_area_group_noted(self):
        groups = {"area:office": area_group("office", [])}
        result = run(AREAS, groups, {})
        self.assertEqual(result[0]["note"], "No radars in this group")

    def test_unknown_group_member_skipped(self):
        groups = {"zone:9": zone("Open-plan", None, ["a", "ghost"])}
        result = run(AREAS, groups, {"a": None})
        self.assertEqual(result, [node("zone:9", "group", "Open-plan", "zone:9", ["a"])])

    def test_unicode_names_sorted_by_name_then_id(self):
        areas = {"1": "Ärzte", "2": "zoo", "3": "Zoo", "4": "Äb"}
        result = run(areas, {}, {"a": "1", "b": "2", "c": "3", "d": "4"})
        self.assertEqual([n["id"] for n in result], ["area:2", "area:3", "area:4", "area:1"])

    def test_radar_in_area_less_group_and_area_group_is_placed_in_both(self):
        groups = {
            "area:office": area_group("office", ["a", "b"]),
            "zone:9": zone("Open-plan", None, ["a"]),
        }
        result = run(AREAS, groups, {"a": "office", "b": "office"})
        self.assertEqual(
            result,
            [
                node("area:office", "area", "Office", "area:office", ["a", "b"]),
                node("zone:9", "group", "Open-plan", "zone:9", ["a"]),
            ],
        )

    def test_devices_sorted_by_name_then_id_within_leaf(self):
        groups = {"zone:9": zone("Open-plan", None, ["a", "b", "c"])}
        names = {"a": "Zed", "b": "alpha", "c": "ZED"}
        result = run(AREAS, groups, dict.fromkeys("abc"), names=names)
        self.assertEqual(result[0]["devices"], ["b", "a", "c"])

    def test_zones_sorted_by_name_not_id_within_area(self):
        groups = {
            "zone:1": zone("Zulu", "office", ["a"]),
            "zone:2": zone("alpha", "office", ["a"]),
            "zone:0": zone("Mid", "office", ["a"]),
        }
        result = run(AREAS, groups, {"a": "office"})
        self.assertEqual([c["id"] for c in result[0]["children"]], ["zone:2", "zone:0", "zone:1"])

    def test_every_device_placed_and_only_known_devices(self):
        matrix = {
            "no groups": ({}, {}, {"a": None}),
            "unknown member": (
                AREAS,
                {"area:office": area_group("office", ["a", "x"])},
                {"a": "office", "b": "lounge"},
            ),
            "mixed": (
                AREAS,
                {
                    "area:office": area_group("office", ["a"]),
                    "zone:1": zone("Desk", "office", ["a", "ghost"]),
                    "zone:2": zone("Gone", "removed", ["b"]),
                    "zone:3": zone("Loose", None, ["c"]),
                },
                {"a": "office", "b": "lounge", "c": None, "d": "removed"},
            ),
            "area-less group and area group": (
                AREAS,
                {
                    "area:office": area_group("office", ["a", "b"]),
                    "zone:9": zone("Open-plan", None, ["a"]),
                },
                {"a": "office", "b": "office"},
            ),
            "area member in zone of other area": (
                AREAS,
                {
                    "area:office": area_group("office", ["a", "b"]),
                    "zone:1": zone("Sofa", "lounge", ["a"]),
                },
                {"a": "office", "b": "lounge"},
            ),
            "removed-area area group": (
                AREAS,
                {"area:gone": area_group("gone", ["a"])},
                {"a": "gone", "b": "office"},
            ),
            "recording disabled inside zones": (
                AREAS,
                {
                    "area:office": area_group("office", ["b"]),
                    "zone:1": zone("Desk", "office", ["a", "b"]),
                    "zone:2": zone("Loose", None, ["a", "c"]),
                },
                {"a": "office", "b": "office", "c": None},
            ),
        }
        for label, (areas, groups, device_areas) in matrix.items():
            with self.subTest(label):
                recording = {key: key not in "ad" for key in device_areas}
                result = run(areas, groups, device_areas, recording)
                self.assertEqual(set(placed(result)), set(device_areas))

    def test_devices_outside_snapshot_are_not_placed(self):
        groups = {"zone:9": zone("Open-plan", None, ["a", "b"])}
        result = run(AREAS, groups, {"a": None, "b": None}, device_ids=["a"])
        self.assertEqual(placed(result), ["a"])


if __name__ == "__main__":
    unittest.main()

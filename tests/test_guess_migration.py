"""One-time migration of stores written before guess runs (storage minor version 2).

Stores are built in the old layout (a code and a confidence in every row) and must load,
convert once, and answer exactly like the old per-row reading (confidence in steps of 5 %)."""

import base64
import json
import random
import struct
import types
import unittest
import zlib
from copy import deepcopy
from unittest.mock import AsyncMock

import test_tuner as harness
from test_guess_runs import BASE, assigned, joined, query_times, random_rows, reference_cells
from tuner_under_test.history import budget, cleanup, codec, guesses, migration, recording
from tuner_under_test.rooms.intervals import Timeline


def old_block(start, rows, version=3):
    """A stored block of the old layout: ``rows`` are ``(offset, energies, code, percent)``;
    version 3 is delta-coded with a guess per row, 2 the same in plain zlib, 1 has no guess."""
    pieces = []
    for offset, energies, code, percent in rows:
        row = struct.pack(">H", offset) + bytes(energies)
        pieces.append(row + (bytes((code, percent)) if version >= 2 else b""))
    raw = b"".join(pieces)
    if version == 3:
        data = codec.encode_payload(raw, 22)
    else:
        data = base64.b64encode(zlib.compress(raw)).decode("ascii")
    return {
        "version": version,
        "start": start,
        "end": start + rows[-1][0],
        "count": len(rows),
        "data": data,
    }


WHOLE_SECOND_GAPS = (5, 6, 6, 6, 7, 11, 12, 13, 30, 3600)  # Rows hold whole-second offsets.


def old_store(rng, devices=2, samples=900):
    """``({device: {...}}, {device: reference rows})`` in the old layout: rows 6 s apart with
    gaps, in blocks of at most 60 (as the integration flushed them)."""
    data, reference = {}, {}
    for number in range(devices):
        rows = random_rows(rng, samples, BASE + number * 7, WHOLE_SECOND_GAPS)
        blocks, group = [], []
        for ts, code, percent in rows:
            if group and (len(group) >= 60 or ts - group[0][0] > 60000):
                blocks.append(group)
                group = []
            group.append((ts, code, percent))
        blocks.append(group)
        history = []
        for group in blocks:
            start = group[0][0]
            history.append(
                old_block(
                    start,
                    [
                        (
                            round(ts - start),
                            [(int(ts) * 7 + k) % 101 for k in range(18)],
                            code,
                            percent,
                        )
                        for ts, code, percent in group
                    ],
                )
            )
        data[f"d{number}"] = {
            "history": history,
            "history_labels": [{"start": BASE, "end": BASE + 600, "state": "present"}],
            "histograms": {"g0_move": {"present": [1] * 101, "not_present": [0] * 101}},
        }
        reference[f"d{number}"] = rows
    return data, reference


def decoded_rows(device):
    return [
        (ts, bytes(row))
        for block in device["history"]
        for ts, row in recording.block_rows(block, float("-inf"))
    ]


class ConversionTests(unittest.TestCase):
    def test_every_old_row_guess_becomes_a_run_that_reads_like_the_row(self):
        """Seeded randomized equivalence on stores in the old layout, with recording gaps and
        guesses that change, vanish and drift."""
        rng = random.Random(7101)
        data, reference = old_store(rng)
        before = deepcopy(data)
        migrated = migration.convert_guesses({"devices": data, "other": 1})
        self.assertEqual(data, before, "the old store is not touched")
        self.assertEqual(migrated["other"], 1)
        total = 0
        for key, device in migrated["devices"].items():
            rows = reference[key]
            self.assertEqual({block["version"] for block in device["history"]}, {4})
            self.assertEqual([ts for ts, _ in decoded_rows(device)], [ts for ts, _, _ in rows], key)
            self.assertEqual(device["history_labels"], data[key]["history_labels"])
            runs = [run for chunk in device["guess_runs"] for run in guesses.decode(chunk)]
            known = guesses.Guesses(runs)
            held = assigned(rows)
            cells = Timeline(reference_cells(held))
            self.assertEqual(known.cells(), joined(cells.spans))
            for t in query_times(rng, rows):
                self.assertEqual(known.at(t), cells.at(t), (key, t))
                total += 1
            for ts, code, percent in held:
                run = known.of_sample(ts)
                expected = (guesses.STATES[code], percent // 5) if code else None
                self.assertEqual(None if run is None else (run[2], run[3]), expected)
                if run:
                    self.assertEqual(run[4], guesses.UNKNOWN, "migrated rows have no known basis")
        self.assertGreater(total, 5000)

    def test_energies_and_times_survive_exactly(self):
        rng = random.Random(7102)
        data, _ = old_store(rng, devices=1, samples=300)
        old = decoded_rows_old(data["d0"])
        migrated = migration.convert_guesses({"devices": data})["devices"]["d0"]
        self.assertEqual(decoded_rows(migrated), old)

    def test_all_block_versions_load_and_damaged_blocks_are_dropped(self):
        good = {
            1: old_block(BASE, [(0, [5] * 18, 0, 0), (6, [6] * 18, 0, 0)], 1),
            2: old_block(BASE + 100, [(0, [7] * 18, 1, 83), (6, [8] * 18, 1, 83)], 2),
            3: old_block(BASE + 200, [(0, [9] * 18, 2, 61), (6, [10] * 18, 2, 61)], 3),
        }
        odd = [
            None,
            "junk",
            {"start": "x"},
            {"version": 3, "start": float("nan"), "count": 1, "data": good[3]["data"]},
            {**good[3], "data": "not base64!"},
            {**good[3], "count": 9},
            {**good[3], "version": 9},
            {**good[2], "start": BASE + 300, "count": -1},
        ]
        device = {"history": [*odd, *good.values()], "label_revision": 4}
        migrated = migration.convert_guesses({"devices": {"a": device}})["devices"]["a"]
        self.assertEqual(
            [ts for ts, _ in decoded_rows(migrated)],
            [BASE, BASE + 6, BASE + 100, BASE + 106, BASE + 200, BASE + 206],
        )
        runs = [run for chunk in migrated["guess_runs"] for run in guesses.decode(chunk)]
        self.assertEqual([run[2:] for run in runs], [("present", 17, 0), ("not_present", 12, 0)])
        self.assertEqual(migrated["label_revision"], 4, "nothing but history and runs changes")

    def test_a_store_already_in_the_new_layout_is_returned_as_it_is(self):
        energies = [(BASE + 6 * i, bytes([20] * 18)) for i in range(5)]
        device = {
            "history": cleanup._encode(energies),
            "guess_runs": guesses.encode([(BASE, BASE + 24, "present", 16, 1)]),
        }
        data = {"devices": {"a": device}}
        again = migration.convert_guesses(data)
        self.assertIs(again["devices"]["a"], device)

    def test_converting_twice_changes_nothing_more(self):
        rng = random.Random(7103)
        data, _ = old_store(rng, devices=2, samples=200)
        once = migration.convert_guesses({"devices": data})
        twice = migration.convert_guesses(once)
        self.assertEqual(twice, once)
        for key in once["devices"]:
            self.assertIs(twice["devices"][key], once["devices"][key])

    def test_empty_missing_and_odd_devices_and_stores_are_left_alone(self):
        for devices in (
            {},
            {"a": {}},
            {"a": {"history": []}},
            {"a": {"history": None}},
            {"a": {"history": "junk"}},
            {"a": None},
            {"a": []},
            {"a": {"history": [{"version": "3"}]}},
        ):
            data = {"devices": devices}
            self.assertEqual(migration.convert_guesses(data), data, devices)
        for data in ({}, {"devices": None}, {"devices": []}, {"other": 1}):
            self.assertIs(migration.convert_guesses(data), data)

    def test_runs_already_held_are_kept_beside_the_ones_from_old_rows(self):
        old = old_block(BASE, [(0, [5] * 18, 1, 90), (6, [5] * 18, 1, 90)], 3)
        kept = [(BASE - 1000, BASE - 900, "not_present", 14, 1)]
        newer = cleanup._encode([(BASE - 950, bytes([20] * 18))])  # The row inside the kept run.
        device = {"history": [*newer, old], "guess_runs": guesses.encode(kept)}
        migrated = migration.convert_guesses({"devices": {"a": device}})["devices"]["a"]
        runs = [run for chunk in migrated["guess_runs"] for run in guesses.decode(chunk)]
        self.assertEqual(runs[0], kept[0])
        self.assertEqual(runs[1][2:], ("present", 18, 0))

    def test_no_row_is_expired_or_thinned_by_the_conversion(self):
        """The conversion does no maintenance: ancient rows and low-confidence guesses stay
        for the normal cleanup to judge."""
        ancient = old_block(1000.0, [(0, [5] * 18, 1, 10), (6, [5] * 18, 1, 10)], 3)
        recent = old_block(BASE, [(0, [5] * 18, 1, 90)], 3)
        migrated = migration.convert_guesses({"devices": {"a": {"history": [ancient, recent]}}})
        self.assertEqual(len(decoded_rows(migrated["devices"]["a"])), 3)


def decoded_rows_old(device):
    rows = []
    for block in device["history"]:
        raw = codec.decode_payload(block["data"], block["version"], block["count"])
        stride = codec.STRIDES[block["version"]]
        for pos in range(0, len(raw), stride):
            offset = struct.unpack(">H", raw[pos : pos + 2])[0]
            rows.append((block["start"] + offset, bytes(raw[pos + 2 : pos + 20])))
    return rows


class StoreMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_store_converts_in_the_executor_and_keeps_to_the_size_ceiling(self):
        calls = []

        async def executor(function, *args):
            calls.append(function.__name__)
            return function(*args)

        store = harness.mod.TunerStore()
        store.hass = types.SimpleNamespace(async_add_executor_job=AsyncMock(side_effect=executor))
        rng = random.Random(7104)
        data, _ = old_store(rng, devices=1, samples=120)
        migrated = await store._async_migrate_func(2, 1, {"devices": data})
        self.assertEqual(calls, ["_migrated"], "one job, off the event loop")
        self.assertTrue(migrated["devices"]["d0"]["guess_runs"])
        self.assertEqual({b["version"] for b in migrated["devices"]["d0"]["history"]}, {4})
        # Saved as it is migrated, the file is the size the budget measures.
        envelope = {
            "version": 2,
            "minor_version": harness.constants.STORAGE_MINOR_VERSION,
            "key": "ld2410_tuner.data",
            "data": migrated,
        }
        self.assertEqual(
            budget.storage_bytes(migrated), len(json.dumps(envelope, indent=2).encode())
        )

    async def test_an_interrupted_migration_leaves_the_old_data_to_migrate_again(self):
        rng = random.Random(7105)
        data, _ = old_store(rng, devices=1, samples=80)
        original = deepcopy(data)
        store = harness.mod.TunerStore()

        async def interrupted(function, *args):
            raise RuntimeError("interrupted")

        store.hass = types.SimpleNamespace(async_add_executor_job=interrupted)
        with self.assertRaises(RuntimeError):
            await store._async_migrate_func(2, 1, {"devices": data})
        self.assertEqual(data, original)

        async def run(function, *args):
            return function(*args)

        store.hass = types.SimpleNamespace(async_add_executor_job=run)
        again = await store._async_migrate_func(2, 1, {"devices": data})
        self.assertTrue(again["devices"]["d0"]["guess_runs"])

    def test_the_store_is_versioned_so_home_assistant_calls_the_migration(self):
        self.assertEqual(harness.constants.STORAGE_MINOR_VERSION, 2)
        self.assertEqual(harness.constants.STORAGE_VERSION, 2)

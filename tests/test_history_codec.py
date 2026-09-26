"""Lossless stored history and legacy migration regressions."""

import base64
import random
import struct
import unittest
import zlib

import test_tuner as harness
from tuner_under_test.history.cleanup import clean_history, decode_payload, encode_payload


class CodecTests(unittest.TestCase):
    def test_all_byte_values_and_timestamp_wraps_round_trip(self):
        rng = random.Random(41)
        for count in (0, 1, 60, 100):
            raw = bytes(rng.randrange(256) for _ in range(count * 22))
            self.assertEqual(decode_payload(encode_payload(raw), 3, count), raw)

    def test_smooth_radar_channels_compress_better_without_losing_spikes(self):
        raw = b"".join(
            struct.pack(">H", index * 6)
            + bytes(
                [100 if index == 20 else (column * 5 + index % 3) % 101 for column in range(18)]
            )
            + bytes([1, 97])
            for index in range(60)
        )
        encoded = encode_payload(raw)
        self.assertLess(len(encoded), len(base64.b64encode(zlib.compress(raw, 6))))
        self.assertEqual(decode_payload(encoded, 3, 60), raw)

    def test_mixed_formats_preserve_labels_and_are_idempotent(self):
        first = harness.HistoryCleanupTests().block(100.25, [(0, [8] * 18)])
        second = harness.HistoryCleanupTests().block(106.75, [(0, [9] * 18 + [2, 83])], 2)
        raw = struct.pack(">H", 0) + bytes([255] + [100] * 17 + [1, 99])
        third = dict(second, version=3, start=112.5, end=112.5, data=encode_payload(raw))
        device = {
            "history": [first, second, third],
            "history_labels": [{"start": 99, "end": 120, "state": "not_present"}],
        }
        cleaned, stats = clean_history(device, [], 130, 1000)
        self.assertEqual(stats["migrated_blocks"], 2)
        self.assertTrue(all(block["version"] == 3 for block in cleaned["history"]))
        self.assertEqual(cleaned["history_labels"], device["history_labels"])
        self.assertEqual(clean_history(cleaned, [], 130, 1000)[0], cleaned)
        self.assertEqual(cleaned["histograms"]["g1_still"]["not_present"][100], 1)

    def test_bad_versions_and_lengths_fail(self):
        encoded = encode_payload(bytes(22))
        for version, count in [(4, 1), (3, 2), (3, -1)]:
            with self.assertRaises(ValueError):
                decode_payload(encoded, version, count)

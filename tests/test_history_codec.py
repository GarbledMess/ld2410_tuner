"""Lossless stored history and legacy migration regressions."""

import base64
import random
import struct
import unittest
import zlib

import test_tuner as harness
from tuner_under_test.history import guesses
from tuner_under_test.history.cleanup import clean_history
from tuner_under_test.history.codec import decode_payload, encode_payload


class CodecTests(unittest.TestCase):
    def test_all_byte_values_and_timestamp_wraps_round_trip(self):
        rng = random.Random(41)
        for count in (0, 1, 60, 100):
            raw = bytes(rng.randrange(256) for _ in range(count * 20))
            self.assertEqual(decode_payload(encode_payload(raw), 4, count), raw)
            # Version 3 rows also held the radar's guess: 22 bytes, read for migration only.
            old = bytes(rng.randrange(256) for _ in range(count * 22))
            self.assertEqual(decode_payload(encode_payload(old, 22), 3, count), old)

    def test_smooth_radar_channels_compress_better_without_losing_spikes(self):
        raw = b"".join(
            struct.pack(">H", index * 6)
            + bytes(
                [100 if index == 20 else (column * 5 + index % 3) % 101 for column in range(18)]
            )
            for index in range(60)
        )
        encoded = encode_payload(raw)
        self.assertLess(len(encoded), len(base64.b64encode(zlib.compress(raw, 6))))
        self.assertEqual(decode_payload(encoded, 4, 60), raw)

    def test_mixed_formats_preserve_labels_and_are_idempotent(self):
        first = harness.HistoryCleanupTests().block(100.25, [(0, [8] * 18)])
        second = harness.HistoryCleanupTests().block(106.75, [(0, [9] * 18 + [2, 83])], 2)
        raw = struct.pack(">H", 0) + bytes([255] + [100] * 17 + [1, 99])
        third = dict(second, version=3, start=112.5, end=112.5, data=encode_payload(raw, 22))
        device = {
            "history": [first, second, third],
            "history_labels": [{"start": 99, "end": 120, "state": "not_present"}],
        }
        cleaned, stats = clean_history(device, [], 130, 1000)
        self.assertEqual(stats["migrated_blocks"], 3)
        self.assertTrue(all(block["version"] == 4 for block in cleaned["history"]))
        # The rows' old guesses are runs now: (not present, 83 % -> 17 steps) and (present, 99 % -> 20).
        runs = [run for chunk in cleaned["guess_runs"] for run in guesses.decode(chunk)]
        self.assertEqual([run[2:] for run in runs], [("not_present", 17, 0), ("present", 20, 0)])
        self.assertEqual(cleaned["history_labels"], device["history_labels"])
        self.assertEqual(clean_history(cleaned, [], 130, 1000)[0], cleaned)
        self.assertEqual(cleaned["histograms"]["g1_still"]["not_present"][100], 1)

    def test_a_later_duplicate_row_replaces_the_earlier_rows_guess_too(self):
        guessed = harness.HistoryCleanupTests().block(100.0, [(0, [8] * 18 + [1, 90])], 2)
        plain = harness.HistoryCleanupTests().block(100.0, [(0, [9] * 18 + [0, 0])], 2)
        device = {"history": [guessed, plain]}
        cleaned, stats = clean_history(device, [], 130, 1000)
        self.assertEqual(stats["duplicate_samples"], 1)
        self.assertEqual(cleaned["guess_runs"], [])
        device = {"history": [plain, guessed]}
        cleaned, _ = clean_history(device, [], 130, 1000)
        self.assertEqual(len([r for c in cleaned["guess_runs"] for r in guesses.decode(c)]), 1)

    def test_column_codec_matches_the_byte_by_byte_codec(self):
        """Equivalence: the C-level column codec gives exactly the bytes of the previous
        byte-by-byte loops, for random rows, sizes and strides, both directions."""

        def old_encode(raw, stride=20):
            columns = bytearray()
            for column in range(stride):
                previous = 0
                for value in raw[column::stride]:
                    columns.append((value - previous) % 256)
                    previous = value
            return base64.b64encode(zlib.compress(bytes(columns), 6)).decode("ascii")

        def old_decode(encoded, count, stride=20):
            raw = zlib.decompress(base64.b64decode(encoded, validate=True))
            restored = bytearray(len(raw))
            for column in range(stride):
                value = 0
                for index in range(count):
                    value = (value + raw[column * count + index]) % 256
                    restored[index * stride + column] = value
            return bytes(restored)

        rng = random.Random(4104)
        for count in (0, 1, 2, 59, 60, 61, 250):
            for _ in range(5):
                raw = rng.randbytes(count * 20)
                self.assertEqual(encode_payload(raw), old_encode(raw))
                self.assertEqual(decode_payload(old_encode(raw), 4, count), raw)
                self.assertEqual(
                    decode_payload(old_encode(raw), 4, count), old_decode(old_encode(raw), count)
                )
        for length in (0, 1, 7, 33, 101):
            raw = rng.randbytes(length)
            for stride in (1, 5, 20, 22):
                self.assertEqual(encode_payload(raw, stride), old_encode(raw, stride))

    def test_bad_versions_and_lengths_fail(self):
        encoded = encode_payload(bytes(20))
        for version, count in [(5, 1), (4, 2), (4, -1), (0, 1)]:
            with self.assertRaises(ValueError):
                decode_payload(encoded, version, count)

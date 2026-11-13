"""Timed search counts each label's seen cells from its own bytes of the mask.

Shifting a whole block mask (one bit per six-second cell, 28 days) once per label allocated a
large integer per label per cost evaluation; reading the label's own byte window gives the
same count. Disk counters are updated per block, adding in the same order.
"""

import random
import struct
import tempfile
from array import array

import test_tuner  # noqa: F401
from tuner_under_test.engine.hybrid import search_timing

CELL = search_timing.CELL_SECONDS


class _Geometry:
    def __init__(self, blocks, labels):
        self.blocks, self._labels = blocks, labels

    def get(self, key):
        return None, self._labels[key[1]], None


def _case(seed):
    rng = random.Random(seed)
    blocks, labels, masks = [], {}, ([], [])
    for b in range(3):
        size = rng.choice((64, 4096, 40000))
        lo = b * 100000
        blocks.append((lo, size))
        for lane in masks:
            lane.append(rng.getrandbits(size))
        found = []
        for _ in range(rng.randint(0, 60)):
            offset = rng.randrange(0, size + 16)
            width = rng.randint(1, 900)
            state = rng.choice(("present", "not_present"))
            source = rng.choice(("human", "auto"))
            found.append(
                ((state, source, rng.randrange(12), rng.random()), offset, (1 << width) - 1)
            )
        labels[lo] = found
    return _Geometry(blocks, labels), masks


def _oracle(geometry, masks, pids):
    counters = [0.0] * (2 * pids)
    for block, (lo, _size) in enumerate(geometry.blocks):
        for (state, source, pid, confidence), offset, bits in geometry.get((None, lo))[1]:
            signal = int(len(masks) > 1 and state == "present" and source != "human")
            seen = ((masks[signal][block] >> offset) & bits).bit_count() * CELL
            counters[2 * pid] += seen
            counters[2 * pid + 1] += seen * confidence
    return counters


def test_resident_counts_equal_the_shifted_mask_counts():
    zone = type("Zone", (), {"group_id": "z"})()
    for seed in range(25):
        geometry, masks = _case(seed)
        for lanes in (masks, masks[:1]):
            counters = array("d", bytes(12 * 16))
            search_timing._add_seen(geometry, zone, lanes, counters)
            assert list(counters) == _oracle(geometry, lanes, 12)


def test_disk_counts_equal_the_shifted_mask_counts():
    zone = type("Zone", (), {"group_id": "z"})()
    for seed in range(25):
        geometry, masks = _case(seed)
        with tempfile.TemporaryFile() as counters:
            counters.truncate(12 * search_timing._SEEN.size)
            search_timing._count_seen(geometry, zone, masks, counters)
            counters.seek(0)
            raw = counters.read()
        found = [v for pair in struct.iter_unpack(search_timing._SEEN.format, raw) for v in pair]
        assert found == _oracle(geometry, masks, 12)

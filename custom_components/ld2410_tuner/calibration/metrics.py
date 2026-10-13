"""Learning metrics."""

from __future__ import annotations

from math import floor

from .constants import SAMPLE_SECONDS

# Byte -> "1" or "0", for each bit of a reading: the bit's samples as a binary number.
PLANE = [bytes(0x31 if value >> bit & 1 else 0x30 for value in range(256)) for bit in range(8)]


def metrics(rows, thresholds):
    return measure_observations(
        (row, any(row[1].get(key, -1) > value for key, value in thresholds.items())) for row in rows
    )


def measure_observations(observations):
    """Count outcomes and episodes identically for raw and timing-adjusted hits."""
    counts = {"present": 0, "not_present": 0}
    hits = {"present": 0, "not_present": 0}
    temporal = _TemporalMetrics()
    for row, hit in sorted(observations, key=lambda item: item[0][0]):
        timestamp, _values, label = row[:3]
        counts[label] += 1
        hits[label] += hit
        temporal.observe(timestamp, label, hit)
    return {
        **temporal.summary(),
        "present_samples": counts["present"],
        "not_present_samples": counts["not_present"],
        "sensitivity": hits["present"] / counts["present"] if counts["present"] else 0.0,
        "false_positive_rate": hits["not_present"] / counts["not_present"]
        if counts["not_present"]
        else 0.0,
        "false_positives": hits["not_present"],
        "false_negatives": counts["present"] - hits["present"],
    }


class _TemporalMetrics:
    def __init__(self):
        self.episodes = self.missed_episodes = self.misses = self.longest_misses = self.bursts = 0
        self.previous = None
        self.episode_hit = self.previous_false = False
        self.absent_seconds = 0.0

    def close_episode(self):
        if self.previous and self.previous[1] == "present" and not self.episode_hit:
            self.missed_episodes += 1

    def observe(self, timestamp, label, hit):
        continuous = (
            self.previous is not None
            and self.previous[1] == label
            and 0 <= timestamp - self.previous[0] <= SAMPLE_SECONDS * 2
        )
        if not continuous:
            self.close_episode()
            self.misses, self.episode_hit, self.previous_false = 0, False, False
            self.episodes += label == "present"
        if label == "present":
            self.episode_hit |= hit
            self.misses = 0 if hit else self.misses + 1
            self.longest_misses = max(self.longest_misses, self.misses)
        else:
            self.observe_absence(timestamp, hit, continuous)
        self.previous = (timestamp, label)

    def observe_absence(self, timestamp, hit, continuous):
        self.absent_seconds += (
            min(SAMPLE_SECONDS, timestamp - self.previous[0]) if continuous else SAMPLE_SECONDS
        )
        self.bursts += bool(hit and not self.previous_false)
        self.previous_false = hit

    def summary(self):
        self.close_episode()
        return {
            "presence_episodes": self.episodes,
            "missed_presence_episodes": self.missed_episodes,
            "longest_missed_run_samples": self.longest_misses,
            "false_trigger_bursts": self.bursts,
            "false_trigger_bursts_per_hour": self.bursts * 3600 / self.absent_seconds
            if self.absent_seconds
            else 0.0,
            "observed_absent_seconds": self.absent_seconds,
        }


def bit_planes(column):
    """The samples of a bytes ``column`` of readings that have each bit of a reading set, as
    bit sets (bit ``i`` for sample ``i``), the lowest bit first: a reading is compared with a
    threshold by 8 bit-set steps for all samples at once (``above``)."""
    if not column:
        return [0] * 8
    return [int(column.translate(PLANE[bit])[::-1], 2) for bit in range(8)]


def above(planes, threshold, everything):
    """The samples that read more than ``threshold`` (strictly, as in the LD2410 protocol),
    from the ``bit_planes`` of their readings; ``everything`` has a bit set for each sample."""
    if threshold != threshold or threshold >= 255:
        return 0
    if threshold < 0:
        return everything
    limit, greater, equal = floor(threshold), 0, everything  # Readings are whole numbers.
    for bit in range(7, -1, -1):
        if limit >> bit & 1:
            equal &= planes[bit]  # Those reading 0 here read less.
        else:
            greater |= equal & planes[bit]  # Those reading 1 here read more.
            equal &= ~planes[bit]
    return greater


def threshold_masks(rows, key):
    """``masks[t]``: bit ``i`` is set when row ``i`` reads more than ``t`` on ``key`` (strictly
    greater, as in the LD2410 protocol). Thresholds with no reading between them share one mask."""
    if hasattr(rows, "planes_of"):  # Readings kept as columns: compared a bit at a time.
        planes, everything = rows.planes_of(key), (1 << len(rows)) - 1
        masks = {}
        return [
            masks.setdefault(mask, mask)
            for mask in (above(planes, t, everything) for t in range(101))
        ]
    size = (len(rows) + 7) // 8
    bins = {}
    for i, row in enumerate(rows):
        values = row[1]
        if key in values:
            bits = bins.get(values[key])
            if bits is None:
                bits = bins[values[key]] = bytearray(size)
            bits[i >> 3] |= 1 << (i & 7)
    masks = [0] * 101
    running = 0
    for threshold in range(100, -1, -1):
        masks[threshold] = running
        if threshold in bins:
            running |= int.from_bytes(bins[threshold], "little")
    return masks

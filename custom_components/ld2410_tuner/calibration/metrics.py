"""Learning metrics."""

from __future__ import annotations

from math import floor

from .constants import (
    AUTO_CLASS_CAP,
    AUTO_WEIGHT,
    MAX_FALSE_BURSTS_PER_HOUR,
    MAX_FPR,
    MAX_MISSED_RUN,
    MIN_CLASS_SAMPLES,
    MIN_RECALL,
    SAMPLE_SECONDS,
)


def metrics(rows, thresholds):
    counts = {"present": 0, "not_present": 0}
    hits = {"present": 0, "not_present": 0}
    for _timestamp, values, label in rows:
        counts[label] += 1
        hits[label] += any(values.get(key, -1) > threshold for key, threshold in thresholds.items())
    return {
        **_temporal_metrics(rows, thresholds),
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


def _temporal_metrics(rows, thresholds):
    measured = _TemporalMetrics()
    for timestamp, values, label in sorted(rows, key=lambda row: row[0]):
        hit = any(values.get(key, -1) > threshold for key, threshold in thresholds.items())
        measured.observe(timestamp, label, hit)
    return measured.summary()


def _episode_masks(positives, negatives):
    """Keep short/quiet labelled episodes visible beside long active sessions."""
    ordered = sorted(
        [(row[0], "present", i) for i, row in enumerate(positives)]
        + [(row[0], "not_present", -1) for row in negatives],
        key=lambda row: row[0],
    )
    episodes, previous = [], None
    for timestamp, label, index in ordered:
        if label == "present":
            if (
                previous is None
                or previous[1] != label
                or timestamp - previous[0] > SAMPLE_SECONDS * 2
            ):
                episodes.append(0)
            episodes[-1] |= 1 << index
        previous = (timestamp, label)
    return [(mask, mask.bit_count()) for mask in episodes]


def _masks(rows, key):
    bins = [0] * 101
    for i, row in enumerate(rows):
        values = row[1]
        if key in values:
            bins[values[key]] |= 1 << i
    masks = [0] * 101
    running = 0
    for threshold in range(100, -1, -1):
        masks[threshold] = running  # Strictly greater, as in the LD2410 protocol.
        running |= bins[threshold]
    return masks


def _weighted_masks(rows, manual_count):
    """Confidence buckets retain fractional weight; never round counts to integers."""
    total = sum(AUTO_WEIGHT * row[3] for row in rows)
    scale = min(1.0, manual_count * AUTO_CLASS_CAP / total) if manual_count and total else 1.0
    groups = {}
    for i, row in enumerate(rows):
        weight = AUTO_WEIGHT * round(row[3], 2) * scale
        groups[weight] = groups.get(weight, 0) | (1 << i)
    return groups, total * scale


def _weight(mask, groups):
    return sum(weight * (mask & bucket).bit_count() for weight, bucket in groups.items())


def _human_ranker(positives, negatives):
    """Rank target violations before refinements, for full and recent evidence.

    Bit links describe actual consecutive observations, not adjacent rows across
    gaps/label changes. A budgeted isolated miss is never a licence to lose an
    entire presence episode or a run of quiet presence.
    """
    windows = []
    starts = [(0, 0)]
    if min(len(positives), len(negatives)) >= MIN_CLASS_SAMPLES:
        starts.append((int(len(positives) * 0.8), int(len(negatives) * 0.8)))
    for pstart, nstart in starts:
        present, absent = positives[pstart:], negatives[nstart:]
        episodes = [mask << pstart for mask, _ in _episode_masks(present, absent)]
        empty_episodes = [mask << nstart for mask, _ in _episode_masks(absent, present)]
        presence_mask = ((1 << len(present)) - 1) << pstart
        absent_mask = ((1 << len(absent)) - 1) << nstart
        presence_links = presence_mask ^ sum(mask & -mask for mask in episodes)
        absent_links = absent_mask ^ sum(mask & -mask for mask in empty_episodes)
        seconds = _temporal_metrics(present + absent, {})["observed_absent_seconds"]
        windows.append(
            (
                presence_mask,
                absent_mask,
                episodes,
                presence_links,
                absent_links,
                floor(len(present) * (1 - MIN_RECALL) + 1e-9),
                floor(len(absent) * MAX_FPR + 1e-9),
                floor(seconds * MAX_FALSE_BURSTS_PER_HOUR / 3600 + 1e-9),
            )
        )

    def rank(detected, false):
        violations = [0] * 5
        refinements = None
        for (
            pmask,
            nmask,
            episodes,
            plinks,
            nlinks,
            miss_budget,
            false_budget,
            burst_budget,
        ) in windows:
            missed = pmask & ~detected
            false_here = nmask & false
            missed_count, false_count = missed.bit_count(), false_here.bit_count()
            bursts = (false_here & ~((false_here << 1) & nlinks)).bit_count()
            # Count runs exceeding the allowed length without scanning samples.
            too_long = missed
            for _ in range(MAX_MISSED_RUN):
                too_long = missed & (too_long << 1) & plinks
            failures = (
                sum(not (detected & mask) for mask in episodes),
                max(0, missed_count - miss_budget),
                too_long.bit_count(),
                max(0, false_count - false_budget),
                max(0, bursts - burst_budget),
            )
            violations = [a + b for a, b in zip(violations, failures, strict=False)]
            if refinements is None:
                refinements = (missed_count, false_count, bursts)
        return (*violations, *refinements)

    return rank

"""Threshold search."""

from __future__ import annotations

from math import floor

from .constants import MAX_FPR, MIN_CLASS_SAMPLES
from .metrics import _human_ranker, _masks, _weight, _weighted_masks
from .separation import gate_preference


def _union_masks(selected, excluded=()):
    combined = [0] * 4
    for key, bits in selected.items():
        if key in excluded:
            continue
        for index, mask in enumerate(bits):
            combined[index] |= mask
    return combined


class _ThresholdSearch:
    """One deterministic search; keeps bit tables and ranking context together."""

    def __init__(self, positives, negatives, automatic, keys, current):
        self.positives, self.negatives, self.keys = positives, negatives, keys
        self.human_calibration = bool(positives and negatives)
        self.ap, self.an = automatic["present"], automatic["not_present"]
        self.pw, self.pmass = _weighted_masks(self.ap, len(positives))
        self.nw, self.nmass = _weighted_masks(self.an, len(negatives))
        self.human_rank = _human_ranker(positives, negatives)
        self.tables, self.preferred, self.quiet = {}, {}, {}
        self.human_gaps = set()
        for key in keys:
            self._prepare_gate(key, (current or {}).get(key, 50))

    def _prepare_gate(self, key, fallback):
        groups = (self.positives, self.negatives, self.ap, self.an)
        noise = sorted(row[1][key] for row in (self.negatives or self.an) if key in row[1])
        observed = any(key in row[1] for group in groups for row in group)
        preference = gate_preference(
            key,
            {"present": self.positives, "not_present": self.negatives},
            {"present": self.ap, "not_present": self.an},
            fallback,
        )
        self.preferred[key] = preference["preferred_threshold"]
        if preference["human_supported"]:
            self.human_gaps.add(key)
        self.quiet[key] = max(noise) if noise else self.preferred[key]
        masks = [_masks(rows, key) for rows in groups]
        candidates = range(101) if observed else [int(fallback)]
        self.tables[key] = {t: tuple(mask[t] for mask in masks) for t in candidates}

    def rank(self, bits, gate_false=0, distance=(0, 0)):
        detected, false, auto_detected, auto_false = bits
        loss = 20 * (1 - _weight(auto_detected, self.pw) / self.pmass) if self.pmass else 0
        loss += _weight(auto_false, self.nw) / self.nmass if self.nmass else 0
        return (
            *self.human_rank(detected, false),
            gate_false if self.human_calibration else 0,
            distance[0],
            round(loss, 10),
            distance[1],
        )

    def _distance(self, thresholds):
        return (
            sum(abs(thresholds[key] - self.preferred[key]) for key in self.human_gaps),
            sum(abs(thresholds[key] - self.preferred[key]) for key in self.keys),
        )

    def _replacement_distance(self, distance, thresholds, updates):
        human, total = distance
        for key, value in updates.items():
            change = abs(value - self.preferred[key]) - abs(thresholds[key] - self.preferred[key])
            total += change
            human += change * (key in self.human_gaps)
        return human, total

    @staticmethod
    def _gate_false(selected):
        return sum(bits[1].bit_count() for bits in selected.values())

    def _gate_step(self, key, thresholds, selected, distance, best_rank, best):
        other = _union_masks(selected, (key,))
        other_false = self._gate_false(selected) - selected[key][1].bit_count()
        for threshold, bits in self.tables[key].items():
            candidate = tuple(a | b for a, b in zip(other, bits, strict=False))
            changed = self._replacement_distance(distance, thresholds, {key: threshold})
            candidate_rank = self.rank(candidate, other_false + bits[1].bit_count(), changed)
            if candidate_rank < best_rank:
                best_rank, best = candidate_rank, (key, threshold, bits)
        return best_rank, best

    def optimize(self, seed):
        thresholds = dict(seed)
        selected = {key: self.tables[key][value] for key, value in thresholds.items()}
        for _step in range(len(self.keys) * 4):
            distance = self._distance(thresholds)
            best_rank, best = (
                self.rank(_union_masks(selected), self._gate_false(selected), distance),
                None,
            )
            for key in self.keys:
                best_rank, best = self._gate_step(
                    key, thresholds, selected, distance, best_rank, best
                )
            if best is None:
                break
            key, threshold, bits = best
            thresholds[key], selected[key] = threshold, bits
        return thresholds, best_rank

    def _distinct_options(self):
        options = {}
        for key in self.keys:
            unique = {}
            for threshold, bits in self.tables[key].items():
                previous = unique.get(bits)
                if previous is None or abs(threshold - self.preferred[key]) < abs(
                    previous - self.preferred[key]
                ):
                    unique[bits] = threshold
            options[key] = [(threshold, bits) for bits, threshold in unique.items()]
        return options

    @staticmethod
    def _supported_lowerings(partial, lost, lowers):
        for lowered, bits in lowers:
            if not bits[0] & lost:
                continue
            candidate = tuple(a | b for a, b in zip(partial, bits, strict=False))
            yield lowered, candidate, bits[1].bit_count()

    def _pair_candidates(self, noisy, support, thresholds, selected, options):
        raises = [
            (t, bits)
            for t, bits in options[noisy]
            if t > thresholds[noisy] and selected[noisy][1] & ~bits[1]
        ]
        lowers = [
            (t, bits)
            for t, bits in options[support]
            if t < thresholds[support] and bits[0] & ~selected[support][0]
        ]
        other = _union_masks(selected, (noisy, support))
        distance = self._distance(thresholds)
        other_false = (
            self._gate_false(selected)
            - selected[noisy][1].bit_count()
            - selected[support][1].bit_count()
        )
        for raised, bits in raises:
            partial = tuple(a | b for a, b in zip(other, bits, strict=False))
            lost = selected[noisy][0] & ~(partial[0] | selected[support][0])
            for lowered, candidate, support_false in self._supported_lowerings(
                partial, lost, lowers
            ):
                changed = self._replacement_distance(
                    distance, thresholds, {noisy: raised, support: lowered}
                )
                yield (
                    self.rank(
                        candidate, other_false + bits[1].bit_count() + support_false, changed
                    ),
                    {**thresholds, noisy: raised, support: lowered},
                )

    def _repair_noisy(self, noisy, thresholds, selected, options, score, best):
        for support in self.keys:
            if support == noisy:
                continue
            for candidate_score, candidate in self._pair_candidates(
                noisy, support, thresholds, selected, options
            ):
                if candidate_score < score:
                    score, best = candidate_score, candidate
        return score, best

    def repair_pair(self, thresholds, score):
        # Preserve the original iteration order and strict tie breaking.
        selected = {key: self.tables[key][thresholds[key]] for key in self.keys}
        options, best = self._distinct_options(), None
        for noisy in self.keys:
            score, best = self._repair_noisy(noisy, thresholds, selected, options, score, best)
        return best

    def _bounded_seed(self):
        negatives = self.negatives
        windows = [((1 << len(negatives)) - 1, floor(len(negatives) * MAX_FPR + 1e-9))]
        if min(len(self.positives), len(negatives)) >= MIN_CLASS_SAMPLES:
            start = int(len(negatives) * 0.8)
            windows.append(
                (
                    ((1 << len(negatives)) - 1) ^ ((1 << start) - 1),
                    floor((len(negatives) - start) * MAX_FPR + 1e-9),
                )
            )
        return {
            key: next(
                t
                for t, bits in options.items()
                if all((bits[1] & mask).bit_count() <= budget for mask, budget in windows)
            )
            for key, options in self.tables.items()
        }

    def _alternate_seeds(self, thresholds, score):
        for seed in (self._bounded_seed(), self.quiet):
            if seed == self.preferred:
                continue
            alternative, alternative_score = self.optimize(seed)
            if alternative_score < score:
                thresholds, score = alternative, alternative_score
        return thresholds, score

    def _needs_refinement(self, score):
        # With no human presence reference, preserve the established inferred
        # fitting balance rather than pursuing ever quieter settings.
        return any(score[:5]) or (self.human_calibration and bool(score[6]))

    def run(self):
        thresholds, score = self.optimize(self.preferred)
        if self._needs_refinement(score):
            thresholds, score = self._alternate_seeds(thresholds, score)
        for _ in range(2):
            if not self._needs_refinement(score):
                break
            repaired = self.repair_pair(thresholds, score)
            if repaired is None:
                break
            thresholds, score = self.optimize(repaired)
        return thresholds, {"present": self.pmass, "not_present": self.nmass}


def _search(positives, negatives, auto_groups, keys, current=None):
    return _ThresholdSearch(positives, negatives, auto_groups, keys, current).run()

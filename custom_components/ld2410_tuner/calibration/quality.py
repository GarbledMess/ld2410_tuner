"""Frozen learner-only reliability and energy-regime policy."""

from dataclasses import dataclass
from itertools import accumulate
from math import isfinite
from types import MappingProxyType

from .constants import ATTRIBUTION_MARGIN, ATTRIBUTION_TAIL_QUANTILE

QUALITY_VERSION = 5
QUALITY_MIN_EMPTY_OBSERVATIONS = 100
DENSITY_PRIOR = 0.01
MINIMUM_LIKELIHOOD_RATIO = 2
OCCUPIED_LOWER_QUANTILE = 0.1


@dataclass(frozen=True)
class GateRegime:
    empty_observations: int
    empty_maximum: int | None
    empty_quantile: int | None
    credible_ceiling: int | None
    occupied_episodes: int
    supported_bins: int
    basis: str
    likelihood_bins: int = 0
    occupied_density_episodes: int = 0
    empty_density_episodes: int = 0


@dataclass(frozen=True)
class QualityModel:
    context: object
    posteriors: object
    regimes: object

    def reliability(self, basis, state):
        return self.posteriors.get((basis, state), 0.75)

    def supports(self, key, energy):
        if energy is None or not 0 <= energy <= 100:
            return False
        regime = self.regimes.get(key)
        if regime is None or regime.empty_maximum is None or energy > regime.empty_maximum:
            return True
        bit = 1 << int(energy)
        if regime.supported_bins & bit:
            return True
        return energy > regime.credible_ceiling and bool(regime.likelihood_bins & bit)

    def correct(self, confidence, basis, state):
        if (
            not isinstance(confidence, (int, float))
            or not isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            raise ValueError("raw confidence must be finite and between zero and one")
        return confidence * self.reliability(basis, state)

    def human_episodes(self, state):
        return len({ep for ep in self.context.episodes if ep.state == state})

    def episode_mass(self, state):
        return self._episode_mass(state, corrected=True)

    def _episode_mass(self, state, *, corrected):
        grouped = {}
        for item in self.context.automatic_episodes:
            if item.episode.state != state:
                continue
            ep = item.episode
            key = (ep.start, ep.end, ep.state, tuple(sorted(set(item.dependencies))))
            value = (
                self.correct(item.confidence, item.basis, state) if corrected else item.confidence
            )
            grouped[key] = max(grouped.get(key, 0), value)
        return sum(grouped.values())

    def audit(self):
        keys = set(self.context.gate_presence) | set(self.context.gate_empty)
        return {
            "quality_version": QUALITY_VERSION,
            "fingerprint": self.context.fingerprint,
            "provenance": "recorded_basis_only",
            "reliability": {
                f"{basis}:{state}": value for (basis, state), value in self.posteriors.items()
            },
            "human_episodes": {
                state: self.human_episodes(state) for state in ("present", "not_present")
            },
            "raw_episode_mass": {
                state: self._episode_mass(state, corrected=False)
                for state in ("present", "not_present")
            },
            "automatic_episode_mass": {
                state: self.episode_mass(state) for state in ("present", "not_present")
            },
            "gate_support": {key: _gate_audit(self.regimes[key]) for key in sorted(keys)},
            "regime_policy": {
                "minimum_empty_observations": QUALITY_MIN_EMPTY_OBSERVATIONS,
                "tail_quantile": ATTRIBUTION_TAIL_QUANTILE,
                "margin": ATTRIBUTION_MARGIN,
                "minimum_occupied_episodes": 2,
                "density_prior": DENSITY_PRIOR,
                "minimum_likelihood_ratio": MINIMUM_LIKELIHOOD_RATIO,
                "occupied_lower_quantile": OCCUPIED_LOWER_QUANTILE,
            },
        }


def learn(context):
    """Each source/state/manual episode contributes one fractional trial."""
    votes = {}
    for row in context.overlaps:
        if row.observed > 0:
            key = (row.episode, row.basis, row.predicted)
            votes[key] = max(votes.get(key, 0), max(0, min(1, row.agreement / row.observed)))
    groups = {}
    for (_, basis, state), success in votes.items():
        groups.setdefault((basis, state), []).append(success)
    posterior = {key: (3 + sum(values)) / (4 + len(values)) for key, values in groups.items()}
    keys = set(context.gate_presence) | set(context.gate_empty)
    regimes = {
        key: _regime(
            context.gate_empty.get(key, ()),
            context.gate_presence.get(key, ()),
            context.gate_occupied_histograms.get(key, ()),
            context.gate_empty_histograms.get(key, ()),
        )
        for key in keys
    }
    return QualityModel(context, MappingProxyType(posterior), MappingProxyType(regimes))


def _regime(empty, occupied, positive_histograms=(), empty_histograms=()):
    total = sum(empty)
    maximum = max((energy for energy, count in enumerate(empty) if count), default=None)
    supported = sum(
        1 << energy
        for energy in range(101)
        if sum(bool(mask & (1 << energy)) for mask in occupied) >= 2
    )
    if total < QUALITY_MIN_EMPTY_OBSERVATIONS:
        basis = "sparse_empty_maximum" if total else "no_empty_prior"
        return GateRegime(total, maximum, None, maximum, len(occupied), supported, basis)
    rank = int((total - 1) * ATTRIBUTION_TAIL_QUANTILE)
    quantile = next(energy for energy, count in enumerate(accumulate(empty)) if count > rank)
    ceiling = min(maximum, quantile + ATTRIBUTION_MARGIN)
    return GateRegime(
        total,
        maximum,
        quantile,
        ceiling,
        len(occupied),
        supported,
        "empirical_empty_tail",
        _likelihood_bins(positive_histograms, empty_histograms),
        sum(sum(hist) > 0 for hist in positive_histograms),
        sum(sum(hist) > 0 for hist in empty_histograms),
    )


def _mean_density(histograms):
    normalized = [tuple(value / sum(hist) for value in hist) for hist in histograms if sum(hist)]
    return (
        tuple(sum(hist[i] for hist in normalized) / len(normalized) for i in range(101))
        if normalized
        else (0.0,) * 101
    )


def _likelihood_bins(positive_histograms, empty_histograms):
    positive, empty = _mean_density(positive_histograms), _mean_density(empty_histograms)
    if not sum(positive) or not sum(empty):
        return 0
    floor = next(
        energy
        for energy, mass in enumerate(accumulate(positive))
        if mass >= OCCUPIED_LOWER_QUANTILE
    )
    return sum(
        1 << energy
        for energy in range(101)
        if energy + ATTRIBUTION_MARGIN >= floor
        and (sum(positive[max(0, energy - 3) : energy + 4]) + DENSITY_PRIOR)
        > MINIMUM_LIKELIHOOD_RATIO * (sum(empty[max(0, energy - 3) : energy + 4]) + DENSITY_PRIOR)
    )


def _gate_audit(regime):
    return {
        "occupied_episodes": regime.occupied_episodes,
        "occupied_supported_bins": regime.supported_bins.bit_count(),
        "empty_observations": regime.empty_observations,
        "empty_maximum": regime.empty_maximum,
        "empty_quantile": regime.empty_quantile,
        "credible_ceiling": regime.credible_ceiling,
        "prior_only": regime.credible_ceiling is None,
        "regime_basis": regime.basis,
        "likelihood_supported_bins": regime.likelihood_bins.bit_count(),
        "occupied_density_episodes": regime.occupied_density_episodes,
        "empty_density_episodes": regime.empty_density_episodes,
    }

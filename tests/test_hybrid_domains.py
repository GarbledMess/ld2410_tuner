"""Legal hybrid domains and pinned numerical transforms."""

from math import floor

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.engine.hybrid.domains import (
    attenuated_threshold,
    evidence_margin,
    normalize_pattern,
)
from tuner_under_test.engine.hybrid.types import RadarSpec


def radar(current=52, domain=tuple(range(0, 99, 7)), channels=(0,), timing=None):
    return RadarSpec("a", channels, {0: domain}, {0: current}, timing or {})


def test_integer_domain_normalization_ties_upward():
    assert normalize_pattern({"a": radar()}) == {"a": {0: 49}}
    assert normalize_pattern({"a": radar(10.5)}) == {"a": {0: 14}}


def test_empty_domain_rejected():
    with pytest.raises(ValueError, match="domain"):
        normalize_pattern({"a": radar(domain=())})


def test_attenuated_threshold_preserves_every_crossing():
    for energy in range(101):
        for threshold in range(101):
            assert (floor(3 * energy / 5) > threshold) == (energy > attenuated_threshold(threshold))


def test_evidence_margin_is_10_point_5_for_two_episodes():
    assert evidence_margin(1, 1) == 10.5
    assert evidence_margin(0, 0) == 20.5


@pytest.mark.parametrize("domain", [(0, 101), (0, 1.5), (7, 0), (0, 0)])
def test_invalid_domain_rejected(domain):
    with pytest.raises(ValueError, match="domain"):
        normalize_pattern({"a": radar(domain=domain)})


def test_unknown_channel_rejected():
    with pytest.raises(ValueError, match="channel"):
        normalize_pattern({"a": radar(channels=(18,))})


@pytest.mark.parametrize("current", [float("nan"), float("inf"), "52"])
def test_invalid_current_rejected(current):
    with pytest.raises(ValueError, match="finite"):
        normalize_pattern({"a": radar(current=current)})


@pytest.mark.parametrize(
    "timing",
    [{"timeout": -1}, {"timeout": float("inf")}, {"timeout": 1, "on_delay": -1, "off_delay": 0}],
)
def test_invalid_timing_rejected(timing):
    with pytest.raises(ValueError, match="Timing"):
        normalize_pattern({"a": radar(timing=timing)})

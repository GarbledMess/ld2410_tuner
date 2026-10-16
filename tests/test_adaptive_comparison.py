"""Fixed-pattern comparison shares learner quality without fitting candidates again."""

from types import SimpleNamespace

import test_tuner
from tuner_under_test.calibration import comparison


def view_with_raw_evidence():
    """Present readings of 40 for 6 minutes and empty ones of 10, each with the radar's own
    guess at 55 % (basis unknown: an old-layout row), and no human labels."""
    rows = [(i * 6, bytes([40] + [255] * 17 + [1, 55])) for i in range(60)]
    rows += [(1000 + i * 6, bytes([10] + [255] * 17 + [2, 55])) for i in range(60)]
    view = SimpleNamespace(
        data={"devices": {"renamed-unit": {"history_labels": []}}}, hass=None, _history_runtime={}
    )
    return test_tuner.record_rows(view, "renamed-unit", rows)


def test_raw_admitted_low_confidence_is_measured_after_quality_attenuation():
    context = {
        "keys": ["g0_move"],
        "timing": {},
        "quality_version": 1,
        "patterns": {
            "live": {"thresholds": {"g0_move": 20}, "applicable": False},
            "automatic": {"id": "candidate", "thresholds": {"g0_move": 20}, "applicable": True},
        },
    }
    found = comparison.calculate(view_with_raw_evidence(), "renamed-unit", context)
    # Six-second units of recorded time; each recording's last reading holds half a sample.
    assert found["counts"]["automatic"] == {"present": 59.5, "not_present": 59.5}
    assert found["quality"]["provenance"] == "recorded_basis_only"
    assert found["patterns"]["automatic"]["score"] == 100
    assert found["patterns"]["automatic"]["automatic"]["duration"]["present_seconds"] > 0

"""Resident memory of the noisy 365-day forced-anomaly stress fit (fresh process)."""

import json
import subprocess
import sys
from pathlib import Path

import test_tuner  # noqa: F401
from tuner_under_test.engine.hybrid.constants import HUMAN_EVIDENCE_HOURS

HERE = Path(__file__).resolve().parent


def test_noisy_year_forced_fit_stays_under_256_mib(tmp_path):
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and repository script
        [sys.executable, "-B", str(HERE / "hybrid_memory_fit.py"), str(tmp_path)],
        cwd=HERE,
        capture_output=True,
        text=True,
        check=True,
        timeout=1800,
    )
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert 0 < result["peak_mib"] < 256
    # The masked re-score really ran: suspects were found and tested.
    assert result["anomaly"]["suspects"] > 0
    assert result["anomaly"]["status"] in ("discarded", "kept: no influence")
    assert result["spilled_bytes"] > 0
    # The human horizon bounds the year: newest labels, the crossing one kept whole.
    hours = result["human_seconds"] / 3600
    assert HUMAN_EVIDENCE_HOURS <= hours <= HUMAN_EVIDENCE_HOURS + 8
    assert result["built_days"] <= 55

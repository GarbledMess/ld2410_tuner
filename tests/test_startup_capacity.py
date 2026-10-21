"""Normal full-day live recording must survive protected startup handoff."""

import datetime
import json
import subprocess
import sys
from array import array
from unittest.mock import patch

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.history import daystore
from tuner_under_test.worker import startup

START = 1_760_486_400.0


def dense_live(rows=172800, guesses=86400):
    live = daystore._Live(datetime.date(2025, 10, 15))
    builder = live.builder
    builder.times = array("d", (START + index / 2 for index in range(rows)))
    builder.ends = array("d", (START + (index + 1) / 2 for index in range(rows)))
    builder.columns = [bytearray([gate]) * rows for gate in range(18)]
    builder.columns[0] = bytearray([12, 13]) * (rows // 2)
    builder.guesses = [
        (START + i, START + i + 1, "present" if i % 2 else "not_present", 20, 1)
        for i in range(guesses)
    ]
    return live


def test_full_day_two_hz_rows_and_one_hz_guesses_roundtrip(tmp_path):
    original = dense_live()
    descriptor = startup.freeze_live("synthetic", original, tmp_path, 0)
    restored = startup.load_live(tmp_path, descriptor)
    assert restored.builder.times == original.builder.times
    assert restored.builder.ends == original.builder.ends
    assert restored.builder.columns == original.builder.columns
    assert restored.builder.guesses == original.builder.guesses


def test_eight_one_hz_radars_fit_raw_and_decoded_startup_budgets():
    descriptor = {"rows": 86400, "guesses": 86400, "confirmed": 0, "floats": [False] * 18}
    startup._budget({"live": [descriptor] * 8})


def test_guess_heavy_handoff_is_rejected_before_reading_any_file(tmp_path):
    descriptor = {"rows": 0, "guesses": 100000, "floats": [False] * 18}
    with patch.object(startup, "_read", side_effect=AssertionError("read before budget")):
        with pytest.raises(ValueError, match="decoded.*actual=.*limit="):
            startup.load({"live": [descriptor] * 11}, tmp_path)


def test_count_failure_reports_counts_and_limits():
    with pytest.raises(ValueError, match="rows=.*limit=.*guesses=.*confirmed="):
        startup._size({"rows": startup.MAX_ROWS + 1, "guesses": 0})


@pytest.mark.parametrize(
    "descriptors,reason",
    [
        ([{"rows": 1, "floats": [False] * 18}] * 129, "many devices"),
        ([{"rows": 600000, "floats": [False] * 18}] * 2, "many rows"),
        ([{"rows": 800000, "floats": [True] * 18}], "large raw day"),
    ],
)
def test_valid_handoffs_are_governed_by_restored_memory_not_old_counts(descriptors, reason):
    startup._budget({"live": descriptors})


def test_decoded_budget_accepts_exact_boundary_and_rejects_next_record():
    descriptor = {"rows": 1, "guesses": 3, "confirmed": 2, "floats": [False] * 18}
    boundary = startup._decoded_size(descriptor)
    with patch.object(startup, "MAX_DECODED_BYTES", boundary):
        startup._budget({"live": [descriptor]})
        with pytest.raises(ValueError, match="decoded.*actual=.*limit="):
            startup._budget({"live": [{**descriptor, "confirmed": 3}]})


@pytest.mark.parametrize("mode,expected", [("storage", 192), ("numerical", 16)])
def test_child_file_limit_matches_storage_handoff_without_changing_cpu_or_memory(mode, expected):
    package = str(startup.__file__.rsplit("worker", 1)[0])
    script = "\n".join(
        [
            "import sys,types,json",
            f"p=types.ModuleType('test_child');p.__path__=[{json.dumps(package)}];sys.modules[p.__name__]=p",
            "from test_child.worker import entrypoint",
            "entrypoint._namespace()",
            "from ld2410_child.worker import limits",
            "class Captured(Exception): pass",
            "def install(budget,**kwargs):",
            " print(json.dumps([budget.memory_bytes,budget.cpu_seconds,budget.file_bytes]));raise Captured",
            "limits.install=install",
            "entrypoint._namespace=lambda:None",
            f"sys.argv=['entrypoint','unused','42']+({['storage'] if mode == 'storage' else []!r})",
            "try: entrypoint.main()",
            "except Captured: pass",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [512 * 1024**2, 120, expected * 1024**2]

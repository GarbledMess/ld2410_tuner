"""Offline numerical entry points must not require importing Home Assistant."""

import subprocess
import sys
from pathlib import Path


def test_device_and_room_numerics_import_in_a_clean_interpreter():
    root = Path(__file__).resolve().parents[1] / "custom_components/ld2410_tuner"
    code = f"""
import sys
import types
package = types.ModuleType('offline_tuner')
package.__path__ = [{str(root)!r}]
sys.modules[package.__name__] = package
from offline_tuner.calibration import event_fitting
from offline_tuner.rooms import assessment
assert callable(event_fitting.fit)
assert callable(assessment.calculate)
assert not any(name.startswith('homeassistant') for name in sys.modules)
print('offline imports ok')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "offline imports ok"

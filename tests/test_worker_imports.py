"""Every graph starts a fresh worker child, so its imports are paid on every chart."""

import json
import subprocess
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "custom_components/ld2410_tuner"


def test_worker_child_does_not_import_the_event_loop_or_thread_pools():
    # asyncio and concurrent.futures served only Home Assistant-side pacing, yet cost
    # the child about 0.1 s of its 0.17 s of imports on a desktop.
    script = "\n".join(
        [
            "import sys, types",
            "p = types.ModuleType('ld2410_child')",
            f"p.__path__ = [{json.dumps(str(PACKAGE))}]",
            "sys.modules[p.__name__] = p",
            "from ld2410_child.worker import calculations, codec, display, protocol",
            "from ld2410_child.presentation import charts",
            "from ld2410_child.history import daystore, pace",
            "loaded = sorted(m for m in ('asyncio', 'concurrent.futures') if m in sys.modules)",
            "assert not loaded, loaded",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", script], capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr.decode()

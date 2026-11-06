"""CPU of a real-shaped Learn: cold, then again after one human label changed.

No private input: rows come from ``tests/real_rows.py``, kept under ``root`` between runs. Results
beside the days are removed first, so the first Learn folds every finished day; the second finds
those days cached, as a Learn does after a human label change. Each Learn is its own process.
"""

import argparse
import cProfile
import json
import pstats
import subprocess  # nosec B404
import sys
from pathlib import Path
from time import process_time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
import test_tuner  # noqa: E402, F401
from real_rows import ROWS, recorded_view  # noqa: E402
from tuner_under_test.calibration.settings import DEFAULT_AUTOMATIC_WEIGHT  # noqa: E402
from tuner_under_test.const import HISTORY_KEYS  # noqa: E402
from tuner_under_test.engine.hybrid import facade  # noqa: E402

KEPT = {".day", ".json"}
CHANGED_LABEL_SECONDS = 600


def _forget_results(root):
    for path in root.rglob("*"):
        if path.is_file() and path.suffix not in KEPT:
            path.unlink()


def _change_label(view, seconds):
    """The newest label, the usual correction: training fits before the cut are unaffected."""
    view.data["devices"]["r"]["history_labels"][-1]["end"] -= seconds


def _learn(args):
    """One Learn in this process, as a worker job: its own view, no state from another Learn."""
    view = recorded_view(args.root, args.rows)
    if args.learn == "repeat":
        _change_label(view, args.change)
    keys = list(HISTORY_KEYS)
    profiler = cProfile.Profile() if args.profile else None
    if profiler:
        profiler.enable()
    start = process_time()
    report = facade.fit(view, "r", keys, dict.fromkeys(keys, 90), None, DEFAULT_AUTOMATIC_WEIGHT)
    seconds = process_time() - start
    if profiler:
        profiler.disable()
        stats = pstats.Stats(profiler, stream=sys.stderr)
        stats.sort_stats("cumtime").print_stats(args.profile)
        stats.sort_stats("tottime").print_stats(args.profile // 2)
    thresholds = {key: value["threshold"] for key, value in report["proposals"].items()}
    print(json.dumps({"seconds": round(seconds, 2), "thresholds": thresholds}))


def _job(args, learn):
    command = [sys.executable, __file__, str(args.root), "--rows", str(args.rows)]
    command += ["--learn", learn, "--profile", str(args.profile), "--change", str(args.change)]
    done = subprocess.run(command, capture_output=True, check=False, text=True)  # nosec B603
    sys.stderr.write(done.stderr)
    if done.returncode:
        raise SystemExit(done.returncode)
    return json.loads(done.stdout.splitlines()[-1])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--rows", type=int, default=ROWS)
    parser.add_argument("--learn", choices=("cold", "repeat"))
    parser.add_argument("--profile", type=int, default=0, help="Print this many profile lines")
    parser.add_argument("--change", type=int, default=CHANGED_LABEL_SECONDS, help="Seconds to cut")
    args = parser.parse_args()
    if args.learn:
        _learn(args)
        return
    recorded_view(args.root, args.rows)
    _forget_results(args.root)
    cold = _job(args, "cold")
    repeat = _job(args, "repeat")
    found = {"cold": cold["seconds"], "repeat": repeat["seconds"]}
    print(json.dumps({**found, "thresholds": repeat["thresholds"]}))


if __name__ == "__main__":
    main()

"""CPU of a full Learn on a synthetic real-shaped radar, cold then warm; no private input."""

import argparse
import cProfile
import json
import pstats
import sys
from pathlib import Path
from time import process_time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
import test_tuner  # noqa: E402, F401
from hybrid_real_shape import recorded  # noqa: E402
from tuner_under_test.const import HISTORY_KEYS  # noqa: E402
from tuner_under_test.engine.hybrid import facade  # noqa: E402

SIDECARS = ("*.sum", "*.res", "*.cell", "*.cols")


def _forget_results(root):
    for pattern in SIDECARS:
        for path in root.rglob(pattern):
            path.unlink()


def _learn(root, days, profile):
    view = recorded(root, days, irregular=True)
    keys = list(HISTORY_KEYS)
    profiler = cProfile.Profile() if profile else None
    if profiler:
        profiler.enable()
    start = process_time()
    report = facade.fit(view, "r", keys, dict.fromkeys(keys, 90), None, 0.0)
    seconds = process_time() - start
    if profiler:
        profiler.disable()
        pstats.Stats(profiler).sort_stats("cumtime").print_stats(45)
    return seconds, report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--profile", action="store_true")
    args = parser.parse_args()
    recorded(args.root, args.days, irregular=True)
    _forget_results(args.root)
    cold, report = _learn(args.root, args.days, args.profile)
    warm, _ = _learn(args.root, args.days, args.profile)
    thresholds = {key: value["threshold"] for key, value in report["proposals"].items()}
    print(json.dumps({"cold": round(cold, 2), "warm": round(warm, 2), "thresholds": thresholds}))


if __name__ == "__main__":
    main()

"""CPU of a real-shaped Learn: cold, then again after one human label changed.

No private input: rows come from ``tests/real_rows.py``, kept under ``root`` between runs. Results
beside the days are removed first, so the first Learn folds every finished day; the second finds
those days cached, as a Learn does after a human label change. Each Learn is its own process.

With ``--room`` the Learn is a linked room of two radars, as ``tests/room_speed_fixture.py`` shapes
it: first, again with nothing changed, again after the newest human label changed, and once more
after a label changed with no remembered fit at hand. Each job is captured as the integration
would capture it; only ``room_fit`` itself is timed.
"""

import argparse
import cProfile
import hashlib
import json
import pstats
import subprocess  # nosec B404
import sys
import tempfile
from pathlib import Path
from time import process_time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
import test_tuner  # noqa: E402, F401
from real_rows import ROWS, recorded_view  # noqa: E402
from room_speed_fixture import request, stored  # noqa: E402
from tuner_under_test.calibration.settings import DEFAULT_AUTOMATIC_WEIGHT  # noqa: E402
from tuner_under_test.const import HISTORY_KEYS  # noqa: E402
from tuner_under_test.engine.hybrid import facade  # noqa: E402
from tuner_under_test.history import worker_snapshot  # noqa: E402
from tuner_under_test.worker import calculations  # noqa: E402

KEPT = {".day", ".json", ".log"}
CHANGED_LABEL_SECONDS = 600
ROOM_DAYS = 44


def _forget_results(root):
    for path in root.rglob("*"):
        if path.is_file() and path.suffix not in KEPT:
            path.unlink()


def _forget_memory(root):
    """What a Learn remembered about whole fits, keeping the days' folded results."""
    for path in root.rglob("*.fit"):
        path.unlink()


def _change_label(view, seconds):
    """The newest label, the usual correction: training fits before the cut are unaffected."""
    view.data["devices"]["r"]["history_labels"][-1]["end"] -= seconds


def _measured(args, call):
    """``call``'s result and CPU seconds, profiled if asked."""
    profiler = cProfile.Profile() if args.profile else None
    if profiler:
        profiler.enable()
    start = process_time()
    report = call()
    seconds = process_time() - start
    if profiler:
        profiler.disable()
        stats = pstats.Stats(profiler, stream=sys.stderr)
        stats.sort_stats("cumtime").print_stats(args.profile)
        stats.sort_stats("tottime").print_stats(args.profile // 2)
    return report, seconds


def _learn(args):
    """One Learn in this process, as a worker job: its own view, no state from another Learn."""
    view = recorded_view(args.root, args.rows)
    if args.learn == "repeat":
        _change_label(view, args.change)
    keys = list(HISTORY_KEYS)
    report, seconds = _measured(
        args,
        lambda: facade.fit(
            view, "r", keys, dict.fromkeys(keys, 90), None, DEFAULT_AUTOMATIC_WEIGHT
        ),
    )
    thresholds = {key: value["threshold"] for key, value in report["proposals"].items()}
    print(json.dumps({"seconds": round(seconds, 2), "thresholds": thresholds}))


def _comparable(value):
    """A report less its evidence fingerprints, which no code reads and the route may change."""
    if isinstance(value, dict):
        return {k: _comparable(v) for k, v in value.items() if k != "fingerprint"}
    if isinstance(value, (list, tuple)):
        return [_comparable(item) for item in value]
    return value


def _changed_room(fixture):
    """The newest human label ten minutes shorter."""
    labels = [dict(label) for label in fixture.labels]
    manual = (label for label in labels if label["source"] == "manual")
    newest = max(manual, key=lambda label: label["start"])
    newest["end"] -= CHANGED_LABEL_SECONDS
    fixture.labels = labels


def _room_learn(args):
    worker_snapshot._executor_only = lambda: None
    fixture = stored(args.root, ROOM_DAYS, sensor_days=args.sensor_days)
    if args.learn == "changed":
        _changed_room(fixture)
    with tempfile.TemporaryDirectory(prefix="room-job-") as folder:
        job = request(
            fixture,
            Path(folder) / "job",
            window_days=args.window_days,
            attribution_days=args.attribution_days,
        )
        report, seconds = _measured(args, lambda: calculations.execute(job))
    text = json.dumps(_comparable(report), sort_keys=True, default=repr)
    found = {
        "seconds": round(seconds, 2),
        "thresholds": report["thresholds"],
        "report": hashlib.sha256(text.encode()).hexdigest(),
    }
    print(json.dumps(found))


def _job(args, learn, *extra):
    command = [sys.executable, __file__, str(args.root), "--rows", str(args.rows)]
    command += ["--learn", learn, "--profile", str(args.profile), "--change", str(args.change)]
    command += ["--window-days", str(args.window_days)]
    command += ["--attribution-days", str(args.attribution_days)]
    command += ["--sensor-days", str(args.sensor_days), *extra]
    done = subprocess.run(  # nosec B603
        command, capture_output=True, check=False, text=True, stdin=subprocess.DEVNULL
    )
    sys.stderr.write(done.stderr)
    if done.returncode:
        raise SystemExit(done.returncode)
    return json.loads(done.stdout.splitlines()[-1])


def _room_main(args):
    worker_snapshot._executor_only = lambda: None
    stored(args.root, ROOM_DAYS, sensor_days=args.sensor_days)
    _forget_results(args.root)
    found = {
        "first": _job(args, "first", "--room"),
        "same": _job(args, "same", "--room"),
        "changed": _job(args, "changed", "--room"),
    }
    _forget_memory(args.root)
    found["changed_unremembered"] = _job(args, "changed", "--room")
    print(json.dumps(found))


def _main_device(args):
    if args.learn:
        _learn(args)
        return
    recorded_view(args.root, args.rows)
    _forget_results(args.root)
    cold = _job(args, "cold")
    repeat = _job(args, "repeat")
    found = {"cold": cold["seconds"], "repeat": repeat["seconds"]}
    print(json.dumps({**found, "thresholds": repeat["thresholds"]}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--rows", type=int, default=ROWS)
    parser.add_argument("--learn", choices=("cold", "repeat", "first", "same", "changed"))
    parser.add_argument("--profile", type=int, default=0, help="Print this many profile lines")
    parser.add_argument("--change", type=int, default=CHANGED_LABEL_SECONDS, help="Seconds to cut")
    parser.add_argument("--room", action="store_true", help="A linked room of two radars")
    parser.add_argument("--window-days", type=int, default=ROOM_DAYS)
    parser.add_argument("--attribution-days", type=int, default=7)
    parser.add_argument("--sensor-days", type=int, default=10)
    args = parser.parse_args()
    if not args.room:
        _main_device(args)
    elif args.learn:
        _room_learn(args)
    else:
        _room_main(args)


if __name__ == "__main__":
    main()

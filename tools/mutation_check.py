#!/usr/bin/env python3
"""Inject realistic bugs one at a time and see whether a test suite notices.

Run from the repository root::

    python tools/mutation_check.py --suite-from HEAD --jobs 4 --output report.json

Each bug is a small source patch, applied to a private copy of the repository's tracked files and
the suite under test; the suite then runs in that copy and stops at its first failure. ``--suite-from``
takes the tests from any git revision, so the old and new suites face the same bugs with the
same source. An unpatched run comes first and each catch is confirmed: the failing test must pass
on the unpatched source, or it is set aside as flaky and the suite runs again without it.

The private ``tests/data`` folder is never copied, read or listed: suites come out of git or from
the tracked files of the working tree, and it is not tracked.
"""

import argparse
import json
import os
import shutil
import subprocess  # nosec B404
import sys
import tarfile
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "custom_components/ld2410_tuner/"
CATALOGUE_TEST = "tests/test_tools.py::MutationCatalogueTests"
MAX_FLAKY = 5


@dataclass(frozen=True)
class Bug:
    name: str
    description: str
    path: str
    old: str
    new: str


BUGS = (
    Bug(
        "label-edge-off-by-one",
        "A label's first instant reads as the label before it",
        "history/label_lookup.py",
        "position = bisect_right(times, timestamp) - 1",
        'position = __import__("bisect").bisect_left(times, timestamp) - 1',
    ),
    Bug(
        "flush-drops-last-record",
        "Each flush to the worker leaves out the newest buffered record",
        "worker/flushing.py",
        "records.append([budget.values[id(record)] for record in live.buffer])",
        "records.append([budget.values[id(record)] for record in live.buffer[:-1]])",
    ),
    Bug(
        "charts-in-learning-lane",
        "Graphs share the learning worker, so they wait behind a long fit",
        "worker/bridge.py",
        'DISPLAY = frozenset({"charts", "recording_rows"})',
        'DISPLAY = frozenset({"recording_rows"})',
    ),
    Bug(
        "flush-in-foreground-lane",
        "Routine day-log flushes queue as interactive work ahead of what the user asked for",
        "runtime/coordinator.py",
        "    @priority.background\n    async def _delayed_flush(self) -> None:",
        "    async def _delayed_flush(self) -> None:",
    ),
    Bug(
        "threshold-outside-radar-range",
        "A radar's advertised threshold range and step are ignored",
        "calibration/thresholds.py",
        "domain = {key: attributes.get(key, value) for key, value in DEFAULT.items()}",
        "domain = dict(DEFAULT)",
    ),
    Bug(
        "present-empty-swapped",
        "A human Empty label trains as Present and the reverse",
        "history/label_lookup.py",
        'events[start].append((-order, end, label.get("state", "unknown")))',
        'events[start].append((-order, end, {"present": "not_present", "not_present": "present"}'
        '.get(label.get("state", "unknown"), "unknown")))',
    ),
    Bug(
        "cleanup-deletes-human-days",
        "Age cleanup applies the automatic retention to human-labelled time",
        "history/budget.py",
        'return now - policy["human_days"] * DAY',
        'return now - policy["automatic_days"] * DAY',
    ),
    Bug(
        "metadata-limit-too-low",
        "The worker metadata limit is below what a real home saves",
        "worker/bridge.py",
        "METADATA_NODES = 150_000",
        "METADATA_NODES = 32_768",
    ),
    Bug(
        "room-union-as-intersection",
        "Present in any one radar's coverage needs every radar's coverage",
        "rooms/evidence.py",
        "    if present:\n        # A confirmed occupied coverage area",
        "    if len(present) == len(labels):\n        # A confirmed occupied coverage area",
    ),
    Bug(
        "save-skipped-at-shutdown",
        "Unload never runs the final save of labels, settings and history",
        "__init__.py",
        "    if worker_stopped:\n        try:\n            async with asyncio.timeout("
        "FINAL_SAVE_TIMEOUT):\n                await transactions.final_save(runtime)",
        "    if False:\n        try:\n            async with asyncio.timeout("
        "FINAL_SAVE_TIMEOUT):\n                await transactions.final_save(runtime)",
    ),
    Bug(
        "learn-eighteen-times-slower",
        "A batching constant makes Learn step through history a few rows at a time",
        "engine/hybrid/constants.py",
        "ROW_BLOCK_ROWS = 4096",
        "ROW_BLOCK_ROWS = 3",
    ),
    Bug(
        "startup-leaves-coverage-open",
        "A crash's open coverage is not ended at the last stored record",
        "history/daystore.py",
        "        if live.is_open():\n            end = dayfile.End(live.last)",
        "        if False:\n            end = dayfile.End(live.last)",
    ),
    Bug(
        "retention-stops-after-one-batch",
        "Day-long cleanup reports done after its first batch",
        "worker/retention.py",
        '"done": len(selected) == len(remaining),',
        '"done": True,',
    ),
    Bug(
        "worker-failure-stops-recording",
        "With the worker quarantined, recorded readings are no longer saved directly",
        "worker/flushing.py",
        '    except Closed:\n        if getattr(runtime, "_worker_closed", False):\n'
        "            raise\n",
        "    except Closed:\n        raise\n",
    ),
    Bug(
        "stale-learn-accepted",
        "A Learn that raced a label edit is saved as if nothing changed",
        "calibration/service.py",
        'if revision != device.get("label_revision", 0):',
        "if False:",
    ),
    Bug(
        "bermuda-reason-swapped",
        "A room without a radar is reported as having recording off, and the reverse",
        "presence/bermuda.py",
        'reason = "recording_off" if radars else "no_radar"',
        'reason = "no_radar" if radars else "recording_off"',
    ),
    Bug(
        "protocol-depth-unbounded",
        "Hostile deeply nested worker messages get through the protocol check",
        "worker/protocol.py",
        "if nodes > max_nodes or depth > 24:",
        "if nodes > max_nodes:",
    ),
    Bug(
        "impossible-reading-recorded",
        "Readings outside 0..100 are recorded instead of refused",
        "history/dayfile.py",
        "if value is not None and not (math.isfinite(value) and 0 <= value <= 100):",
        "if value is not None and not math.isfinite(value):",
    ),
    Bug(
        "memory-limit-not-installed",
        "The worker's address-space limit is left out of the mandatory controls",
        "worker/limits.py",
        '            "memory_bytes": (resource.RLIMIT_AS, budget.memory_bytes),\n',
        "",
    ),
    Bug(
        "relabel-keeps-old-revision",
        "Editing a saved label does not invalidate what was learned from it",
        "history/labels.py",
        '    device["label_revision"] = device.get("label_revision", 0) + 1\n'
        "    runtime._compact_history_labels(device)",
        "    runtime._compact_history_labels(device)",
    ),
    Bug(
        "recording-off-ignored",
        "A radar with recording switched off keeps recording",
        "history/ingest.py",
        '        and device.get("recording_enabled", True)\n',
        "",
    ),
    Bug(
        "sensor-expiry-drops-manual-labels",
        "Expiring old automatic room labels also removes the human ones",
        "rooms/room_labels.py",
        'kept = [x for x in labels if x.get("source") != "sensor" or float(x["end"]) >= cutoff]',
        'kept = [x for x in labels if float(x["end"]) >= cutoff]',
    ),
    Bug(
        "false-triggers-not-counted",
        "Scoring stops pricing false triggers, so noisy thresholds win",
        "engine/hybrid/selection.py",
        'total += FALSE_TRIGGER_SECONDS * metrics["false_triggers"]',
        "total += 0",
    ),
    Bug(
        "trim-removes-human-first",
        "Trim to a size limit removes human-labelled time before automatic time",
        "history/budget.py",
        "for _human, _confidence, low, key, date, high in sorted(_day_candidates(store, data)):",
        "for _human, _confidence, low, key, date, high in sorted(\n"
        "        _day_candidates(store, data), key=lambda item: (not item[0], *item[1:])\n    ):",
    ),
    Bug(
        "export-omits-open-day",
        "A radar's export leaves out the day still being recorded",
        "presentation/export_files.py",
        "            if day is None:\n                continue\n"
        "            raw = dayfile.encode_day(day)\n",
        "            continue\n",
    ),
)


def patched(text, bug):
    """``text`` with ``bug`` applied; its target must occur exactly once."""
    if text.count(bug.old) != 1:
        raise ValueError(f"{bug.name}: patch target occurs {text.count(bug.old)} times")
    return text.replace(bug.old, bug.new)


def _tracked_paths():
    # A list of arguments and a fixed program: no shell involved.
    listed = subprocess.run(  # nosec B603 B607
        ["git", "-C", str(ROOT), "ls-files"], check=True, capture_output=True, text=True
    )
    return listed.stdout.splitlines()


def _copy_paths(paths, target):
    for relative in paths:
        source = ROOT / relative
        if source.is_file():
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)


def _extract_suite(revision, target):
    archive = target / "suite.tar"
    with archive.open("wb") as stream:
        # A list of arguments and a fixed program: no shell involved.
        subprocess.run(  # nosec B603 B607
            ["git", "-C", str(ROOT), "archive", revision], check=True, stdout=stream
        )
    with tarfile.open(archive) as tar:
        tar.extractall(target, filter="data")
    archive.unlink()


def _prepare(base, suite):
    """This checkout's tracked files, with ``suite``'s tests when one is named, in ``base``."""
    tracked = _tracked_paths()
    if suite is None:
        _copy_paths(tracked, base)
        return
    # The whole revision first: its tests may use files this checkout has since removed.
    _extract_suite(suite, base)
    _copy_paths([path for path in tracked if not path.startswith("tests/")], base)


def _first_failure(output):
    for line in output.splitlines():
        if line.startswith(("FAILED ", "ERROR ")):
            return line.split(" - ")[0].strip()
    return None


def run_suite(base, arguments, timeout, *, stop_at_first=True):
    """``(passed, first failing test, seconds)`` of the suite in ``base``."""
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        *(("-x",) if stop_at_first else ()),
        "-p",
        "no:cacheprovider",
        "--basetemp",
        # Outside the copy: the validator tool refuses output inside its own repository.
        str(base.parent / f"{base.name}-pytest"),
        "-m",
        "not scaling",
        # It checks the patches apply, so it would "catch" each of them.
        "--deselect",
        CATALOGUE_TEST,
        *arguments,
    ]
    started = time.perf_counter()
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        # Runs this interpreter's pytest on a private copy of the tree.
        done = subprocess.run(  # nosec B603
            command,
            cwd=base,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=environment,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "(timed out)", time.perf_counter() - started
    seconds = time.perf_counter() - started
    return done.returncode == 0, _first_failure(done.stdout), seconds


def _test_id(failure):
    return failure.split(" ", 1)[1] if failure and " " in failure else None


def _fails_without_the_bug(base, path, original, patched_text, test_id, timeout):
    """Whether ``test_id`` also fails on the unpatched source: then it says nothing of the bug."""
    path.write_text(original, encoding="utf8", newline="\n")
    try:
        passed, _failure, _seconds = run_suite(base, ["-n", "0", test_id], timeout)
    finally:
        path.write_text(patched_text, encoding="utf8", newline="\n")
    return not passed


def _confirmed_run(base, path, original, patched_text, arguments, timeout):
    """The suite against the bug, setting aside any failure that the unpatched source shares
    (a test flaking on a loaded machine must not count as catching a bug)."""
    flaky, seconds = [], 0.0
    while len(flaky) < MAX_FLAKY:
        deselected = [item for test in flaky for item in ("--deselect", test)]
        passed, failure, spent = run_suite(base, [*arguments, *deselected], timeout)
        seconds += spent
        test_id = _test_id(failure)
        if passed or test_id is None:
            return passed, failure, seconds, flaky
        if not _fails_without_the_bug(base, path, original, patched_text, test_id, timeout):
            return False, failure, seconds, flaky
        flaky.append(test_id)
    return None, None, seconds, flaky  # Undecided: too much failed without the bug as well.


def check_bug(bug, suite, work, arguments, timeout):
    """One bug against one suite, in a private copy of everything."""
    base = Path(tempfile.mkdtemp(prefix=f"{bug.name[:20]}-", dir=work))
    try:
        _prepare(base, suite)
        path = base / PACKAGE / bug.path
        original = path.read_text(encoding="utf8")
        text = patched(original, bug)
        path.write_text(text, encoding="utf8", newline="\n")
        passed, failure, seconds, flaky = _confirmed_run(
            base, path, original, text, arguments, timeout
        )
        return {
            "bug": bug.name,
            "description": bug.description,
            "caught": None if passed is None else not passed,
            "first_failure": failure,
            "set_aside_as_flaky": flaky,
            "seconds": round(seconds, 1),
        }
    finally:
        shutil.rmtree(base, ignore_errors=True)
        shutil.rmtree(base.parent / f"{base.name}-pytest", ignore_errors=True)


def baseline(suite, work, arguments, timeout):
    """The unpatched suite, run to the end: everything failing here is listed."""
    base = Path(tempfile.mkdtemp(prefix="baseline-", dir=work))
    try:
        _prepare(base, suite)
        passed, failure, seconds = run_suite(base, arguments, timeout, stop_at_first=False)
        return {"passed": passed, "first_failure": failure, "seconds": round(seconds, 1)}
    finally:
        shutil.rmtree(base, ignore_errors=True)
        shutil.rmtree(base.parent / f"{base.name}-pytest", ignore_errors=True)


def _options(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite-from", help="git revision for tests (default: working tree)")
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=3600, help="seconds per suite run")
    parser.add_argument("--work", default=None, help="folder for the private copies")
    parser.add_argument("--only", action="append", help="bug name (repeatable)")
    parser.add_argument("--output", help="write the report as JSON here")
    parser.add_argument("--no-baseline", action="store_true")
    parser.add_argument("pytest_arguments", nargs="*", help="extra arguments after --")
    return parser.parse_args(argv)


def main(argv=None):
    options = _options(argv)
    work = Path(options.work or tempfile.mkdtemp(prefix="mutation-"))
    work.mkdir(parents=True, exist_ok=True)
    arguments = options.pytest_arguments
    report = {"suite": options.suite_from or "working tree"}
    if not options.no_baseline:
        report["baseline"] = baseline(options.suite_from, work, arguments, options.timeout)
        print("baseline", report["baseline"], flush=True)
    chosen = [bug for bug in BUGS if not options.only or bug.name in options.only]

    def one(bug):
        found = check_bug(bug, options.suite_from, work, arguments, options.timeout)
        print(found["bug"], found["caught"], found["first_failure"], found["seconds"], flush=True)
        return found

    with ThreadPoolExecutor(options.jobs) as pool:
        report["bugs"] = list(pool.map(one, chosen))
    report["caught"] = sum(item["caught"] is True for item in report["bugs"])
    report["total"] = len(report["bugs"])
    print(f"caught {report['caught']} of {report['total']}", flush=True)
    if options.output:
        Path(options.output).write_text(json.dumps(report, indent=2) + "\n", encoding="utf8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

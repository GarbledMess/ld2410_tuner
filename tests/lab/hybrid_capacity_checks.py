"""Full-density synthetic worker capacity; exit 1 records a missed resource gate.

Run with --output outside the checkout. Each case builds independent raw history;
fixture generation and capture are outside the worker CPU and traced-peak timers.
CPU targets are desktop targets. A constrained x86 lab is not an ARM Green timing
prediction; the report records its actual platform and cgroup CPU quota.
"""

import argparse
import json
import os
import platform
import runpy
import tempfile
from pathlib import Path

CASES = {
    "cpu30": {"radars": 30, "days": 28},
    "warm7": {"radars": 7, "days": 28, "warm": True},
    "memory30": {"radars": 30, "days": 28, "trace": True},
    "day1": {"radars": 1, "days": 1, "trace": True},
    "day7": {"radars": 1, "days": 7, "trace": True},
    "day28": {"radars": 1, "days": 28, "trace": True},
    "dense28": {"radars": 1, "days": 28, "trace": True, "density": 2},
}


def budget_failures(name, result):
    """Use the pinned budgets, with no retry, scaling factor or tolerance on CPU."""
    if name in ("cpu30", "warm7"):
        budget = 10 if name == "cpu30" else 2
        return [f"cpu_seconds > {budget}"] if result["cpu_seconds"] > budget else []
    return ["traced_peak_bytes >= 67108864"] if result["traced_peak_bytes"] >= 64 * 1024**2 else []


def environment():
    quota = Path("/sys/fs/cgroup/cpu.max")
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "cgroup_cpu_max": quota.read_text().strip() if quota.is_file() else None,
        "timing_note": "Desktop CPU budgets; constrained x86 is not cycle-accurate Green emulation",
    }


def limit_failures(result):
    """Linux capacity evidence must use every pinned production worker limit."""
    expected = {
        "memory_bytes": 512 * 1024**2,
        "cpu_seconds": 120,
        "file_bytes": 128 * 1024**2,
        "open_files": 64,
    }
    actual = result.get("limits")
    if actual is None:
        return ["Linux worker limits missing"]
    return [
        f"Linux worker limit differs: {key}" for key in expected if actual.get(key) != expected[key]
    ]


def memory_scaling(cases):
    """Report measured growth and enforce fixed budgets, never per-row allowances.

    Growth is descriptive: a completed matrix bounded by these absolute budgets
    is required before interpreting a plateau. Jobs without completed metrics
    cannot supply memory evidence; completed over-budget peaks remain visible.
    Digest payload growth is descriptive too: the cache's byte
    budget is not a budget for every temporary digest alive during folding.
    Traced peak includes Python objects and the rest of the full fit job.
    """
    names = ("day1", "day7", "day28", "dense28")
    fields = {"source_rows", "traced_peak_bytes", "resident_digest_bytes"}
    unavailable = [name for name in names if not fields.issubset(cases.get(name, {}))]
    if unavailable:
        return {"status": "incomplete", "unavailable": unavailable}
    failures = [
        f"{name}: {reason}" for name in names for reason in budget_failures(name, cases[name])
    ]
    peaks = {name: cases[name]["traced_peak_bytes"] for name in names}
    payloads = [cases[name]["resident_digest_bytes"] for name in names]
    return {
        "status": "failed" if failures else "passed",
        "failures": failures,
        "source_rows": {name: cases[name]["source_rows"] for name in names},
        "traced_peak_bytes": peaks,
        "peak_growth_bytes": {name: peaks[name] - peaks["day1"] for name in names[1:]},
        "resident_digest_range_bytes": [min(payloads), max(payloads)],
    }


def job_failures(result):
    checks = [
        (
            all(result[key] for key in ("isolated", "no_site", "no_bytecode")),
            "child isolation flags missing",
        ),
        (result["legality"], "illegal thresholds"),
        (0 < result["max_row_block"] <= 4096, "raw block exceeds 4096 rows"),
        (result["resident_digests_at_end"] == 0, "digests retained after job"),
        (result["open_spill_files"] == 0, "spill files retained after job"),
    ]
    if "cold" in result:
        checks.append(
            (
                all(
                    result[key] == result["cold"][key]
                    for key in ("thresholds", "cost_before", "cost_after")
                ),
                "cold/warm fitting results differ",
            )
        )
    return [reason for valid, reason in checks if not valid]


def measure(root, output, cases, run, *, resource_checks=None):
    """Persist each result so a later failure cannot erase earlier measurements."""
    report = {
        "environment": environment(),
        "resource_checks": resource_checks,
        "cases": {},
        "passed": resource_checks is None or resource_checks["status"] != "failed",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    for name in cases:
        print(f"Measuring {name}: {CASES[name]}", flush=True)
        try:
            result = run(root / name, **CASES[name])
            failures = budget_failures(name, result) + job_failures(result)
            if platform.system() == "Linux":
                failures.extend(limit_failures(result))
            entry = {"status": "failed" if failures else "passed", "failures": failures, **result}
        except Exception as error:
            entry = {"status": "failed", "error": str(error), "type": type(error).__name__}
        report["cases"][name] = entry
        report["passed"] = report["passed"] and entry["status"] == "passed"
        report["memory_scaling"] = memory_scaling(report["cases"])
        report["passed"] = report["passed"] and report["memory_scaling"]["status"] != "failed"
        output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
        print(json.dumps({"case": name, **entry}), flush=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", choices=CASES, action="append", dest="cases")
    args = parser.parse_args(argv)
    harness = runpy.run_path(str(Path(__file__).resolve().parents[1] / "hybrid_scaling.py"))
    harness["_namespace"]()
    resource_checks = {"status": "unsupported", "reason": "Kernel limit probes require Linux"}
    if platform.system() == "Linux":
        try:
            resource_checks = {"status": "passed", **harness["run_limit_checks"]()}
        except Exception as error:
            resource_checks = {
                "status": "failed",
                "error": str(error),
                "type": type(error).__name__,
            }
    with tempfile.TemporaryDirectory(prefix="hybrid-capacity-") as directory:
        report = measure(
            Path(directory),
            args.output,
            args.cases or CASES,
            harness["run_scaling"],
            resource_checks=resource_checks,
        )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

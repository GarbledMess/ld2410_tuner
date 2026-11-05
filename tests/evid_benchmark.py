"""Manual synthetic stage benchmark; never reads recorded user history."""

import cProfile
import datetime
import subprocess
import sys
import types
from array import array
from contextlib import contextmanager
from pathlib import Path
from time import process_time
from types import SimpleNamespace

import test_tuner  # noqa: F401
from tuner_under_test.calibration import event_quality
from tuner_under_test.const import HISTORY_KEYS
from tuner_under_test.engine.hybrid import digest, quality
from tuner_under_test.engine.hybrid.types import RowBlock
from tuner_under_test.history import daystore


class Days:
    def __init__(self, count):
        self.count = count

    def days(self, _device):
        return [datetime.date(1970, 1, 1) + datetime.timedelta(days=i) for i in range(self.count)]

    def guess_runs(self, _device, date, until=None):
        lo, hi = daystore.bounds(date)
        return [
            (
                lo + i,
                min(lo + i + 1200, hi),
                "present" if i % 2400 == 0 else "not_present",
                18,
                i % 3,
            )
            for i in range(0, 86400, 1200)
        ]

    @contextmanager
    def open_columns(self, _device, date):
        self.low, self.high = daystore.bounds(date)
        yield self

    def blocks(self, lo, hi, channels):
        first, after = int((lo - self.low) / 1.125), int((hi - self.low) / 1.125)
        for a in range(first, after, 4096):
            b = min(after, a + 4096)
            t = array("d", (self.low + i * 1.125 for i in range(a, b)))
            e = array("d", (v + 1.125 for v in t))
            cols = tuple(
                bytes((i // 11 + c * 3) % 101 for i in range(a, b))
                if c in channels
                else bytes([255]) * (b - a)
                for c in range(18)
            )
            yield RowBlock(t, e, cols)


def run(count=1, gates=3, legacy=False):
    days = Days(count)
    end = count * 86400
    labels = [
        {
            "start": i + 0.25,
            "end": i + 800.5,
            "state": "present" if i % 2400 == 0 else "not_present",
        }
        for i in range(0, end, 1200)
    ]
    view = SimpleNamespace(
        _days=days,
        _history_until=end,
        _attribution_until=end,
        data={"devices": {"r": {"history_labels": labels}}},
    )
    if legacy:

        def previous(module):
            relative = Path(module.__file__).relative_to(Path(__file__).resolve().parents[1])
            code = subprocess.check_output(["git", "show", "HEAD:" + relative.as_posix()])
            found = types.ModuleType(module.__name__ + "_evid_original")
            found.__package__ = module.__package__
            sys.modules[found.__name__] = found
            exec(compile(code, str(relative), "exec"), found.__dict__)
            return found

        from tuner_under_test.engine.hybrid import physical_index

        old_physical, old_digest = previous(physical_index), previous(digest)
        old_quality, old_event = previous(quality), previous(event_quality)
        old_digest.PhysicalBuilder = old_physical.PhysicalBuilder
        digest.fold_day = old_digest.fold_day
        quality.collect_quality = old_quality.collect_quality
        event_quality._observed = old_event._observed
        event_quality._source_evidence = old_event._source_evidence
    if len(sys.argv) > 4 and sys.argv[4] == "full":
        if legacy:
            from tuner_under_test.engine.hybrid import adapters
            from tuner_under_test.history import fit_days

            adapters.collect_quality = old_quality.collect_quality
            adapters.learn_quality = old_quality.learn_quality
            adapters.fold_day = old_digest.fold_day
            old_days = previous(fit_days)
            old_days.fold_day = old_digest.fold_day
            fit_days.FittingDays = old_days.FittingDays
            adapters.FittingDays = old_days.FittingDays
        full_run(count, gates, legacy)
        return
    start = process_time()
    context = quality.collect_quality(view, "r", HISTORY_KEYS[:gates], 0, end, None, None)
    collected = process_time() - start
    start = process_time()
    for date in days.days("r"):
        lo, hi = daystore.bounds(date)
        with days.open_columns("r", date) as columns:
            result = digest.fold_day(
                columns.blocks(lo, hi, tuple(range(gates))), lo, hi, tuple(range(gates))
            )
            result.partial.close()
            result.physical.close()
    folded = process_time() - start
    print(
        {
            "days": count,
            "rows": count * 76800,
            "gates": gates,
            "legacy": legacy,
            "quality_cpu": collected,
            "fold_cpu": folded,
            "fingerprint": context.fingerprint,
        }
    )


def full_run(count, gates, legacy):
    import hashlib
    import json
    import tempfile

    from tuner_under_test.engine.hybrid import facade
    from tuner_under_test.history import dayfile
    from tuner_under_test.worker import history
    from tuner_under_test.worker.calculations import ReadOnlyDays

    with tempfile.TemporaryDirectory(
        prefix="evid-", dir=Path(__file__).resolve().parents[1]
    ) as root:
        root = Path(root)
        inputs = root / "synthetic"
        inputs.mkdir()
        entries, labels = [], []
        days = Days(count)
        for number, date in enumerate(days.days("r")):
            lo, hi = daystore.bounds(date)
            times = array("d", (lo + i * 1.125 for i in range(76800)))
            ends = array("d", (v + 1.125 for v in times))
            if number == count - 1:
                times, ends = times[:-1000], ends[:-1000]
            columns = [
                bytes(
                    (60 if (int(t) // 1200) % 2 == 0 else 10) + i % 3 for i, t in enumerate(times)
                )
                if c < gates
                else bytes([255]) * len(times)
                for c in range(18)
            ]
            runs = days.guess_runs("r", date)
            day = dayfile.Day(lo, hi, times, ends, columns, runs)
            path = inputs / (date.isoformat() + ".day")
            path.write_bytes(dayfile.encode_day(day))
            entries.append(
                {
                    "device_id": "r",
                    "date": date.isoformat(),
                    "path": str(path),
                    "identity": history._identity(path.stat()),
                    "late": [],
                    "persistent": number < count - 1,
                }
            )
            labels.extend(
                {
                    "start": lo + i + 0.25,
                    "end": lo + i + 800.5,
                    "state": "present" if i % 2400 == 0 else "not_present",
                }
                for i in range(0, 86400, 1200)
            )
        request = {
            "days": entries,
            "budgets": dict.fromkeys(
                ("max_file_bytes", "max_raw_bytes", "max_rows", "max_guesses")
            ),
        }
        request["budgets"]["max_raw_bytes"] = 1 << 30
        view = SimpleNamespace(
            data={
                "devices": {"r": {"history_labels": labels, "training_state": "unknown"}},
                "room_snapshot": None,
            },
            hass=None,
            _days=ReadOnlyDays(request),
            _history_until=count * 86400 - 1125,
            _attribution_until=count * 86400 - 1125,
        )
        keys = HISTORY_KEYS[:gates]
        reports = []
        single = len(sys.argv) > 5
        profiling = single and sys.argv[5] != "once"
        for kind in ("cold",) if single else ("cold", "warm", "changed_labels"):
            if kind == "changed_labels":
                view.data["devices"]["r"]["history_labels"] = [
                    *labels,
                    {
                        "start": count * 86400 - 3000,
                        "end": count * 86400 - 2000,
                        "state": "unknown",
                    },
                ]
            profiler = cProfile.Profile() if profiling else None
            if profiler is not None:
                profiler.enable()
            start = process_time()
            report = facade.fit(view, "r", keys, dict.fromkeys(keys, 90), None, 0.0)
            elapsed = process_time() - start
            if profiler is not None:
                profiler.disable()
                profiler.dump_stats(sys.argv[5])
            encoded = json.dumps(report, separators=(",", ":")).encode()
            output = Path("C:/Users/jackw/Documents/DevEnvironment/codex-finish/tmp")
            (output / f"evid-report-{count}-{legacy}-{kind}.json").write_bytes(encoded)
            reports.append(encoded)
            print(
                {
                    "kind": kind,
                    "days": count,
                    "rows": count * 76800 - 1000,
                    "gates": gates,
                    "legacy": legacy,
                    "cpu": elapsed,
                    "report_sha256": hashlib.sha256(encoded).hexdigest(),
                },
                flush=True,
            )
        if len(reports) > 1:
            assert reports[0] == reports[1]


if __name__ == "__main__":
    args = [int(sys.argv[1]), int(sys.argv[2]), sys.argv[3] == "old"]
    if len(sys.argv) > 4 and sys.argv[4] != "full":
        cProfile.runctx("run(*args)", globals(), locals(), sys.argv[4])
    else:
        run(*args)

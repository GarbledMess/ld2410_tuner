"""Cold full fit of the noisy 365-day forced-anomaly stress case; prints one JSON line.

Run as a fresh process (``python -B hybrid_memory_fit.py DIRECTORY``) so its peak
resident set is the fit's own. Days are generated on demand into DIRECTORY, so
only days the evidence horizons reach are ever built. No private data is read.
"""

import json
import struct
import sys
import time
from math import ceil, floor
from pathlib import Path

import hybrid_noisy
import test_tuner  # noqa: F401
from tuner_under_test.engine.hybrid.digest import DayDigest
from tuner_under_test.engine.hybrid.engine import fit
from tuner_under_test.engine.hybrid.physical_index import PhysicalRows
from tuner_under_test.engine.hybrid.types import (
    EvidencePolicy,
    FitProblem,
    RadarSpec,
    TruthSpan,
    ZoneSpec,
)

RADAR = 5  # a strong radar of the prototype bench (S0-S2 are deliberately weak)
DAYS = 365
CHANNELS = tuple(range(18))


def peak_mib():
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("faults", wintypes.DWORD),
                ("peak", ctypes.c_size_t),
                *((f"field{i}", ctypes.c_size_t) for i in range(7)),
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        process = ctypes.WinDLL("kernel32").GetCurrentProcess
        process.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi")
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        psapi.GetProcessMemoryInfo(process(), ctypes.byref(counters), counters.cb)
        return counters.peak / 2**20
    import resource

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


class Days:
    """Noisy radar-days built once into the work directory, one resident at a time."""

    def __init__(self, root, zeroed):
        self.root, self.zeroed, self.built = root, zeroed, 0
        self._day = None

    def dates(self, rid):
        return iter(range(DAYS))

    def bounds(self, day):
        start = hybrid_noisy.START + day * hybrid_noisy.DAY
        return start, start + hybrid_noisy.DAY

    def _path(self, day):
        return self.root / f"d{day}.bin"

    def _build(self, day):
        k0, cells, cov, times, ends, columns = hybrid_noisy.radar_day(RADAR, day)
        rows = PhysicalRows(CHANNELS, times, ends, dict(zip(CHANNELS, columns, strict=True)))
        encoded = rows.encode()
        self._path(day).write_bytes(
            struct.pack("<qI", k0, len(encoded)) + b"".join(cells) + cov + encoded
        )
        self.built += 1

    def load(self, day):
        if self._day is not None and self._day[0] == day:
            return self._day[1]
        if not self._path(day).exists():
            self._build(day)
        data = self._path(day).read_bytes()
        k0, size = struct.unpack_from("<qI", data)
        n, at = hybrid_noisy.CELLS, 12
        cells = tuple(data[at + c * n : at + (c + 1) * n] for c in range(18))
        cov = data[at + 18 * n : at + 19 * n]
        physical = PhysicalRows.decode(CHANNELS, data[at + 19 * n : at + 19 * n + size])
        self._day = day, (k0, cells, cov, physical)
        return self._day[1]

    def digest(self, rid, day, channels):
        k0, cells, cov, physical = self.load(day)
        if self.zeroed is not None:
            a, b = self.zeroed
            lo, hi = max(0, ceil(a / 6) - k0), min(len(cov), floor(b / 6) - k0)
            if hi > lo:
                cells = tuple(c[:lo] + bytes(hi - lo) + c[hi:] for c in cells)
        return DayDigest(6, k0, len(cov), cells, cov, {}, None, physical)


class Rows:
    def __init__(self, days):
        self.days = days

    def _parts(self, lo, hi):
        for day in range(DAYS):
            a, b = self.days.bounds(day)
            if a < hi and b > lo:
                yield self.days.load(day)[3], max(a, lo), min(b, hi)

    def rows_in(self, lo, hi):
        return sum(rows.rows_in(a, b) for rows, a, b in self._parts(lo, hi))

    def window(self, lo, hi):
        parts = [rows.window(a, b) for rows, a, b in self._parts(lo, hi)]
        if len(parts) == 1:
            return parts[0]
        times, ends = parts[0][0], parts[0][1]
        columns = {c: bytearray(v) for c, v in parts[0][2].items()}
        for t, e, values in parts[1:]:
            times.extend(t)
            ends.extend(e)
            for c, v in values.items():
                columns[c] += v
        return times, ends, {c: bytes(v) for c, v in columns.items()}


def truth():
    """Human labels on every day; on the final day a phone-left entity Present."""
    spans, zeroed = [], None
    for day in range(DAYS):
        for i, (a, b, state) in enumerate(hybrid_noisy.labels_for(day)):
            episode = f"{day}-{i}"
            if day == DAYS - 1 and zeroed is None and state == "not_present" and b - a >= 900:
                zeroed = a + 120, a + 720
                spans += [
                    TruthSpan(a, a + 120, state, "human", 1.0, episode, ()),
                    TruthSpan(a + 120, a + 720, "present", "entity", 1.0, episode + "e", ()),
                    TruthSpan(a + 720, b, state, "human", 1.0, episode, ()),
                ]
            else:
                spans.append(TruthSpan(a, b, state, "human", 1.0, episode, ()))
    return tuple(spans), zeroed


def main():
    root = Path(sys.argv[1])
    spans, zeroed = truth()
    days = Days(root, zeroed)
    radar = RadarSpec(
        "r", CHANNELS, dict.fromkeys(CHANNELS, tuple(range(101))), dict.fromkeys(CHANNELS, 50), {}
    )
    start = hybrid_noisy.START
    problem = FitProblem(
        {"r": radar},
        (ZoneSpec("room", ("r",), spans),),
        start,
        start + DAYS * hybrid_noisy.DAY,
        days,
        {"r": Rows(days)},
        EvidencePolicy(0, False, {}),
    )
    began = time.process_time()
    result = fit(problem)
    d = result.diagnostics
    print(
        json.dumps(
            {
                "cpu": time.process_time() - began,
                "peak_mib": peak_mib(),
                "built_days": days.built,
                "scoring_mode": d["scoring_mode"],
                "spilled_bytes": d["spilled_bytes"],
                "human_seconds": d["evidence"]["human_seconds"],
                "anomaly": {k: d["anomaly"][k] for k in ("status", "suspects")},
            }
        )
    )


if __name__ == "__main__":
    main()

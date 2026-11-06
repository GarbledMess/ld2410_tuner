"""Rows shaped like a real recorder's, for diagnostics work counts and benchmarks.

No private input: everything comes from one seeded generator. Unlike the six-second grids
elsewhere in these tests, times are never on a clock: most rows are short, each starts where
the last ended apart from rare gaps, and many repeat the row before. By default a radar has
2.1 million rows over 44 days, in blocks of about 4,000. Of those rows 30% match the previous
row on the first ten channels and 80% on the first two; 0.2% have one missing channel; 0.1%
follow a gap. A day has 248 automatic labels with edges off any clock, and a few long human
labels overlay them.

Run ``python tests/real_rows.py`` to print the fixture's shape and diagnostics CPU seconds.
"""

import datetime
import json
import random
from array import array
from bisect import bisect_left, bisect_right
from types import SimpleNamespace

import test_tuner  # noqa: F401
from tuner_under_test.engine.hybrid.types import (
    EvidencePolicy,
    FitProblem,
    RadarSpec,
    RowBlock,
    ScorePlan,
    TruthSpan,
    ZoneSpec,
)

ROWS = 2_100_000
DAYS = 44
BLOCK_ROWS = 3962
LABELS_A_DAY = 248
ALL_CHANNELS = 18
THRESHOLD = 40
EPOCH = 1_790_000_000.37
LOW = bytes(i % 36 for i in range(256))
HIGH = bytes(41 + i % 50 for i in range(256))


def _seconds(rng):
    u = rng.random()
    if u < 0.5:
        return rng.uniform(0.05, 0.5)
    if u < 0.75:
        return rng.uniform(0.95, 1.05)
    if u < 0.81:
        return rng.uniform(1.9, 2.1)
    if u < 0.91:
        return rng.uniform(5.9, 6.1)
    if u < 0.94:
        return rng.uniform(6.9, 7.1)
    return rng.uniform(7.5, 9.0)


class _Readings:
    """Row vectors with the real share of repeats and persistent over-threshold bursts."""

    def __init__(self, rng, channels, flip):
        self.rng, self.channels, self.flip = rng, channels, flip
        self.strong = False
        self.head = self._low(2)
        self.tail = self._tail(False)

    def _low(self, count):
        return self.rng.randbytes(count).translate(LOW)

    def _tail(self, strong):
        tail = bytearray(self._low(self.channels - 2))
        if strong:
            for _ in range(self.rng.randint(1, 3)):
                tail[self.rng.randrange(len(tail))] = HIGH[self.rng.randrange(256)]
        return bytes(tail)

    def next(self, occupied):
        u = self.rng.random()
        if u < 0.3:
            return self.head + self.tail
        wanted = self.rng.random() < (0.85 if occupied else 0.1)
        if wanted != self.strong and self.rng.random() < self.flip:
            self.strong = wanted
        self.tail = self._tail(self.strong)
        if u >= 0.8:
            self.head = self._low(2)
        return self.head + self.tail


def _with_missing(rng, vector):
    broken = bytearray(vector)
    broken[rng.randrange(len(broken))] = 255
    return bytes(broken)


def _block(rng, readings, at, rows, label_seconds, channels):
    times, ends, vectors = array("d"), array("d"), []
    for _ in range(rows):
        if rng.random() < 0.001:
            at += rng.uniform(1, 30)
        vector = readings.next(int(at / label_seconds) % 2 == 0)
        if rng.random() < 0.002:
            vector = _with_missing(rng, vector)
        times.append(at)
        at += _seconds(rng)
        ends.append(at)
        vectors.append(vector)
    columns = tuple(map(bytes, zip(*vectors, strict=True)))
    unused = (b"\xff" * rows,) * (ALL_CHANNELS - channels)
    return RowBlock(times, ends, columns + unused), at


def _clipped(block, first, stop, lo, hi):
    times, ends = block.times[first:stop], block.ends[first:stop]
    times[0], ends[-1] = max(times[0], lo), min(ends[-1], hi)
    return RowBlock(times, ends, tuple(column[first:stop] for column in block.columns))


class RealRows:
    """A RowSource over pre-built blocks, clipped to the window as the real reader does."""

    def __init__(self, blocks):
        self.stored = blocks

    def blocks(self, lo, hi):
        for block in self.stored:
            first = bisect_right(block.ends, lo)
            stop = bisect_left(block.times, hi)
            if first < stop:
                yield _clipped(block, first, stop, lo, hi)

    def rows_in(self, lo, hi):
        return sum(len(block.times) for block in self.blocks(lo, hi))


def _span(start, end, state, source, episode):
    return TruthSpan(start, end, state, source, 1.0 if source == "human" else 0.9, episode, ())


def _automatic(rng, start, end, label_seconds):
    edge, index, spans = start, int(start / label_seconds), []
    while edge < end:
        stop = min(end, (index + 1) * label_seconds + rng.uniform(-9, 9))
        state = "present" if index % 2 == 0 else "not_present"
        spans.append(_span(edge, stop, state, "entity", f"a{index}"))
        edge, index = stop, index + 1
    return spans


def _human(rng, start, end):
    spans = []
    for k in range(6):
        at = start + (end - start) * (k + 0.5) / 6 + rng.uniform(-3000, 3000)
        state = "present" if k % 2 == 0 else "not_present"
        spans.append(_span(at, at + rng.uniform(5000, 15000), state, "human", f"h{k}"))
    return spans


def real_problem(
    rows=ROWS, channels=ALL_CHANNELS, flip=0.5, seed=2410, human=("present", "not_present")
):
    """A one-radar problem and its pattern; fewer rows cover proportionally fewer days."""
    rng = random.Random(seed)
    label_seconds = 86400 / LABELS_A_DAY
    readings = _Readings(rng, channels, flip)
    blocks, at, left = [], EPOCH, rows
    while left:
        count = min(left, BLOCK_ROWS)
        block, at = _block(rng, readings, at, count, label_seconds, channels)
        blocks.append(block)
        left -= count
    labels = [span for span in _human(rng, EPOCH, at) if span.state in human]
    truth = _automatic(rng, EPOCH, at, label_seconds) + labels
    picked = tuple(range(channels))
    radar = RadarSpec("r", picked, dict.fromkeys(picked, tuple(range(101))), {}, {})
    problem = FitProblem(
        {"r": radar},
        (ZoneSpec("z", ("r",), tuple(truth)),),
        EPOCH,
        at,
        None,
        {"r": RealRows(blocks)},
        EvidencePolicy(0, False, {}),
    )
    return problem, {"r": dict.fromkeys(picked, THRESHOLD)}


def plan(rows):
    return ScorePlan("exact", (), rows, 2)


DAY = 86400
GUESS_LEVEL = 18


def _day_rows(blocks, start, end):
    """One day's rows at the millisecond resolution a day file keeps."""
    times, ends = array("d"), array("d")
    columns = [bytearray() for _ in range(ALL_CHANNELS)]
    for block in RealRows(blocks).blocks(start, end):
        times.extend(round(value, 3) for value in block.times)
        ends.extend(round(value, 3) for value in block.ends)
        for column, part in zip(columns, block.columns, strict=True):
            column += part
    return times, ends, [bytes(column) for column in columns]


def _day_guesses(truth, start, end):
    return [
        (max(s.start, start), min(s.end, end), s.state, GUESS_LEVEL, 0)
        for s in truth
        if s.source != "human" and s.start < end and s.end > start
    ]


def _write_days(folder, problem):
    from tuner_under_test.history import dayfile

    blocks, truth = problem.rows["r"].stored, problem.zones[0].truth
    first = int(problem.start // DAY)
    last = int(problem.end // DAY)
    for day in range(first, last + 1):
        start, end = day * DAY, (day + 1) * DAY
        times, ends, columns = _day_rows(blocks, start, end)
        physical = dayfile.Day(start, end, times, ends, columns, _day_guesses(truth, start, end))
        (folder / f"{day}.day").write_bytes(dayfile.encode_day(physical))
    return first, last


def _human_rows(problem):
    return [
        {"start": s.start, "end": s.end, "state": s.state}
        for s in problem.zones[0].truth
        if s.source == "human"
    ]


def _write_recording(folder, rows):
    folder.mkdir(parents=True, exist_ok=True)
    problem, _ = real_problem(rows)
    first, last = _write_days(folder, problem)
    meta = {"first": first, "last": last, "until": problem.end, "labels": _human_rows(problem)}
    (folder / "recording.json").write_text(json.dumps(meta), encoding="utf8", newline="\n")
    return meta


def _day_entry(folder, day, last):
    from tuner_under_test.worker import history

    path = folder / f"{day}.day"
    return {
        "device_id": "r",
        "date": str(datetime.date(1970, 1, 1) + datetime.timedelta(days=day)),
        "path": str(path),
        "identity": history._identity(path.stat()),
        "late": [],
        "persistent": day < last,
    }


def recorded_view(root, rows=ROWS):
    """A worker view of this shape's rows as day files under ``root``, the newest day open."""
    from tuner_under_test.worker.calculations import ReadOnlyDays

    folder = root / "r"
    saved = folder / "recording.json"
    if saved.exists():
        meta = json.loads(saved.read_text(encoding="utf8"))
    else:
        meta = _write_recording(folder, rows)
    entries = [
        _day_entry(folder, day, meta["last"]) for day in range(meta["first"], meta["last"] + 1)
    ]
    budgets = {"max_file_bytes": None, "max_raw_bytes": 1 << 34, "max_rows": None}
    return SimpleNamespace(
        data={
            "devices": {"r": {"history_labels": meta["labels"], "training_state": "unknown"}},
            "room_snapshot": None,
        },
        hass=None,
        _days=ReadOnlyDays({"days": entries, "budgets": {**budgets, "max_guesses": None}}),
        _history_until=meta["until"],
        _attribution_until=meta["until"],
    )


def shape(problem):
    """Measured shape: rows, blocks, contiguous and repeat shares, decision changes."""
    channels = problem.radars["r"].channels
    source = problem.rows["r"].stored
    rows = joined = same_10 = same_2 = missing = changes = 0
    for block in source:
        vectors = list(zip(*(block.columns[c] for c in channels), strict=True))
        rows += len(vectors)
        joined += sum(map(float.__eq__, block.times[1:], block.ends))
        same_10 += sum(a[:10] == b[:10] for a, b in zip(vectors, vectors[1:], strict=False))
        same_2 += sum(a[:2] == b[:2] for a, b in zip(vectors, vectors[1:], strict=False))
        missing += sum(255 in v for v in vectors)
        hits = [any(THRESHOLD < x != 255 for x in v) for v in vectors]
        changes += sum(map(bool.__ne__, hits[1:], hits))
    pairs = rows - len(source)
    return {
        "rows": rows,
        "blocks": len(source),
        "days": (problem.end - problem.start) / 86400,
        "labels": len(problem.zones[0].truth),
        "contiguous": joined / pairs,
        "same_10_channels": same_10 / pairs,
        "same_2_channels": same_2 / pairs,
        "missing": missing / rows,
        "decision_changes": changes / pairs,
    }


def _main():
    import argparse
    import cProfile
    import pstats
    import time

    from tuner_under_test.engine.hybrid import diagnostics

    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=ROWS)
    parser.add_argument("--channels", type=int, default=ALL_CHANNELS)
    parser.add_argument("--flip", type=float, default=0.5)
    parser.add_argument("--human", default="present,not_present")
    parser.add_argument("--profile", action="store_true")
    args = parser.parse_args()
    human = tuple(args.human.split(","))
    problem, pattern = real_problem(args.rows, args.channels, args.flip, human=human)
    print(shape(problem))
    before = time.process_time()
    if args.profile:
        profile = cProfile.Profile()
        profile.runcall(diagnostics.diagnostics_pair, problem, pattern, plan(args.rows))
        pstats.Stats(profile).sort_stats("tottime").print_stats(18)
    else:
        diagnostics.diagnostics_pair(problem, pattern, plan(args.rows))
    print(f"diagnostics_pair CPU seconds: {time.process_time() - before:.2f}")


if __name__ == "__main__":
    _main()

"""Cutting time ranges out of finished day files (thinning, Trim now)."""

import math


def cut(day, cuts):
    """Copy a columnar day minus whole time ranges, preserving held vectors at restarts."""
    from array import array
    from bisect import bisect_left, bisect_right

    from . import dayfile

    windows = list(dayfile.gaps(day.start, day.end, _cut_union(cuts)))
    times, ends = array("d"), array("d")
    columns = [array("f") if isinstance(column, array) else bytearray() for column in day.columns]
    runs = []
    for low, high in windows:
        first, after = bisect_right(day.ends, low), bisect_left(day.times, high)
        if first >= after:
            continue
        begin = len(times)
        times.extend(day.times[first:after])
        ends.extend(day.ends[first:after])
        times[begin], ends[-1] = max(times[begin], low), min(ends[-1], high)
        for out, source in zip(columns, day.columns, strict=True):
            out.extend(source[first:after])
        runs.extend(
            (max(a, low), min(b, high), *rest)
            for a, b, *rest in day.guesses
            if b > low and a < high
        )
    return dayfile.Day(
        day.start,
        day.end,
        times,
        ends,
        [bytes(c) if isinstance(c, bytearray) else c for c in columns],
        runs,
    )


def _cut_union(cuts):
    result = []
    for low, high in sorted(cuts):
        if not math.isfinite(low) or not math.isfinite(high) or high < low:
            raise ValueError("Invalid history cut")
        if high == low:
            continue
        if result and low <= result[-1][1]:
            result[-1] = (result[-1][0], max(high, result[-1][1]))
        else:
            result.append((low, high))
    return result

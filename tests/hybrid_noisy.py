"""Seeded noisy synthetic radar history for hybrid stress tests (no private data).

A port of the 2026-10 exp2 noisy stress fixture, vectorized so a long history can
be built quickly: half-second physical rows on all 18 channels, per-channel noise
plus a slow per-minute walk, occupied bursts whose strength varies by channel, rare
empty-room spikes and occasional outages that drop whole cells. One shared
occupancy schedule per day drives the 06:00-14:00 human labels.
"""

import random
from array import array
from itertools import compress
from operator import add

DAY = 86400
START = 1_790_000_000 // DAY * DAY
ROWS_PER_CELL = 12  # half-second rows
CELLS = DAY // 6


def _table(lo, hi):
    span = hi - lo + 1
    return bytes(lo + (v % span) for v in range(256))


def schedule(day, seed=0):
    rng = random.Random(f"sched-{seed}-{day}")
    out, t = [], rng.uniform(0, 3600)
    while t < DAY:
        t += rng.uniform(1800, 4 * 3600)
        duration = rng.uniform(600, 2 * 3600)
        if t >= DAY:
            break
        out.append((t, min(DAY, t + duration)))
        t += duration
    return out


def labels_for(day, seed=0, window=(6 * 3600, 14 * 3600)):
    """Human labels for one day: the labelled window from the schedule."""
    start, (a, b) = START + day * DAY, window
    out, t = [], a
    for x, y in schedule(day, seed):
        x, y = max(x, a), min(y, b)
        if y <= x:
            continue
        if x > t:
            out.append((start + t, start + x, "not_present"))
        out.append((start + x, start + y, "present"))
        t = y
    if t < b:
        out.append((start + t, start + b, "not_present"))
    return out


def _profile(radar, seed):
    rng = random.Random(f"radar-{seed}-{radar}")
    strength = rng.choice([1.0, 1.0, 0.7, 0.4])
    out = []
    for c in range(18):
        gate, still = divmod(c, 2)
        base = rng.randint(2, 10) + (8 if still else 0) + (6 if gate < 2 else 0)
        amp = rng.randint(3, 10)
        lo = int((20 + rng.randint(0, 25) - gate * 3) * strength)
        hi = int(min(100, (50 + rng.randint(0, 45) - gate * 4) * strength))
        out.append((base, amp, max(0, lo), max(lo, hi), rng.random() < 0.6))
    return out


def _column(rng, profile, spans, n):
    base, amp, lo, hi, active = profile
    walk = [0]
    for _ in range(1440):
        walk.append(min(12, max(0, walk[-1] + rng.choice((-1, 0, 1)))))
    drift = b"".join(bytes([w]) * 120 for w in walk[:1440])
    # base + amp + walk <= 46, so no clamp to 100 is needed here.
    values = bytearray(map(add, rng.randbytes(n).translate(_table(base, base + amp)), drift))
    if active:
        burst = rng.randbytes(n).translate(_table(lo, hi))
        for a, b in spans:
            values[a:b] = bytes(map(max, values[a:b], burst[a:b]))
    for _ in range(rng.randint(0, 6)):
        j = rng.randrange(n)
        values[j] = min(100, values[j] + rng.randint(10, 40))
    return values


def radar_day(radar, day, seed=0):
    """``(k0, cells, coverage, times, ends, columns)`` of one noisy radar-day."""
    rng = random.Random(f"noisy-{seed}-{radar}-{day}")
    n = DAY * 2
    spans = [(int(a * 2), min(n, int(b * 2))) for a, b in schedule(day, seed)]
    columns = [_column(rng, p, spans, n) for p in _profile(radar, seed)]
    covered = bytearray(b"\x01") * CELLS
    if rng.random() < 0.3:  # an outage drops whole cells (a coverage break)
        a, length = rng.randrange(CELLS - 100), rng.randint(5, 100)
        covered[a : a + length] = bytes(length)
    keep = bytes(flag for flag in covered for _ in range(ROWS_PER_CELL))
    start = START + day * DAY
    times = array("d", compress((start + i / 2 for i in range(n)), keep))
    ends = array("d", (t + 0.5 for t in times))
    columns = [bytes(compress(col, keep)) for col in columns]
    positions = [i for i, flag in enumerate(covered) if flag]
    cells = []
    for col in columns:
        full = bytearray([255]) * CELLS
        pooled = bytes(map(max, *(col[k::ROWS_PER_CELL] for k in range(ROWS_PER_CELL))))
        for at, value in zip(positions, pooled, strict=True):
            full[at] = value
        cells.append(bytes(full))
    return start // 6, tuple(cells), bytes(covered), times, ends, columns

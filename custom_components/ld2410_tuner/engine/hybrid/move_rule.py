"""Move thresholds read from the gap between empty-room move noise and arrival bursts.

Move's job is catching people as they arrive, before still builds up. Each move gate is placed
a third of the way from the empty room's noise top to the burst a real arrival makes on it,
clear of both edges. A gate that never rises above its noise at arrivals catches no one and is
switched off. No search runs: one read of the trusted empty time and the arrival windows.

Empty evidence: human empty labels (the gold standard, a dog included) and automatic empty
guesses with their edges trimmed (guesses flip late, so their edges hold the arrival or the
departure) whose still reading does not look occupied. Noise is a high quantile over half-hour
pieces, so a few wrong pieces cannot set it. A radar in a room counts as empty only while every
zone it covers is.
"""

from bisect import bisect_right
from dataclasses import dataclass, replace
from re import finditer

from ...calibration.event_replay import _timing
from .constants import CELL_SECONDS
from .digest import cell_ceil, cell_floor

PIECE_SECONDS = 1800.0
EDGE_TRIM_SECONDS = 120.0
SHORTEST_SECONDS = 300.0
NOISE_QUANTILE = 0.99
GAP_SHARE = 1 / 3
ARRIVAL_AFTER_EMPTY = 900.0
ARRIVAL_WINDOW = 60.0
FLOOR_QUANTILE = 0.1
OCCUPIED_ABOVE_FLOOR = 30
_ABOVE = tuple(bytes(1 if t < v <= 100 else 0 for v in range(256)) for t in range(101))
_RUN = b"\x01+"  # a run of detected cells
_UNUSABLE = bytes(range(101, 256))  # cell maxima above 100: no reading (MISSING and floats)


def _merged(spans):
    out = []
    for a, b in sorted(spans):
        if b <= a:
            continue
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _intersect(one, other):
    out, i, j = [], 0, 0
    while i < len(one) and j < len(other):
        a, b = max(one[i][0], other[j][0]), min(one[i][1], other[j][1])
        if a < b:
            out.append((a, b))
        if one[i][1] < other[j][1]:
            i += 1
        else:
            j += 1
    return out


def _complement(spans):
    """Merged ``spans``' gaps on the whole line."""
    edges = [-float("inf")] + [x for span in spans for x in span] + [float("inf")]
    return [(edges[i], edges[i + 1]) for i in range(0, len(edges), 2) if edges[i] < edges[i + 1]]


def _subtract(spans, cut):
    return _intersect(_merged(spans), _complement(_merged(cut)))


def _zone_empty(zone):
    """(trusted empty, of which human) for one zone."""
    human = _merged((s.start, s.end) for s in zone.truth if s.source == "human")
    human_empty = _merged(
        (s.start, s.end) for s in zone.truth if s.source == "human" and s.state == "not_present"
    )
    automatic = _merged(
        (s.start, s.end) for s in zone.truth if s.source != "human" and s.state == "not_present"
    )
    trimmed = [
        (a + EDGE_TRIM_SECONDS, b - EDGE_TRIM_SECONDS)
        for a, b in _subtract(automatic, human)
        if b - a - 2 * EDGE_TRIM_SECONDS >= SHORTEST_SECONDS
    ]
    return _merged(human_empty + trimmed), human_empty


def _radar_empty(zones):
    trusted, human = None, []
    for zone in zones:
        empty, zone_human = _zone_empty(zone)
        trusted = empty if trusted is None else _intersect(trusted, empty)
        human = _merged(human + zone_human)
    trusted = trusted or []
    from_human = _intersect(trusted, human)
    return from_human, _subtract(trusted, from_human)


def _arrivals(zones):
    starts = set()
    for zone in zones:
        present = _merged((s.start, s.end) for s in zone.truth if s.state == "present")
        starts.update(a for a, _b in present)
    return sorted(starts)


def _inner(spans):
    """``spans`` shrunk to whole six-second cells: an edge cell also holds the other state."""
    found = []
    for a, b in spans:
        a, b = cell_ceil(a, CELL_SECONDS) * CELL_SECONDS, cell_floor(b, CELL_SECONDS) * CELL_SECONDS
        if a < b:
            found.append((a, b))
    return found


def _pieces(spans):
    out = []
    for a, b in spans:
        while a < b:
            out.append((a, min(b, a + PIECE_SECONDS)))
            a += PIECE_SECONDS
    return out


def _stats(columns):
    return {c: (max(v), sorted(v)[len(v) // 2]) for c, v in columns.items() if v}


def _read_all(problem, radar_id, channels, queries, present):
    """Per query ``(lo, hi)``: ``{channel: (max, median)}`` of its six-second cell maxima, and
    the raw cells over the merged ``present`` spans as ``(first cell time, {channel: bytes})``.

    One chronological pass over the radar's day digests, one day resident at a time.
    """
    order = sorted(range(len(queries)), key=lambda i: queries[i][0])
    found = [{c: bytearray() for c in channels} for _ in queries]
    segments, first = [], 0
    for date in sorted(problem.days.dates(radar_id)):
        digest = problem.days.digest(radar_id, date, channels)
        if digest is None:
            continue
        low = digest.k0 * digest.resolution
        while first < len(order) and queries[order[first]][1] <= low:
            first += 1
        _fold(digest, channels, [(i, queries[i], found[i]) for i in order[first:]])
        segments += _cells(digest, channels, present)
    return [_stats(columns) for columns in found], segments


def _bounds(digest, lo, hi):
    res, k0 = digest.resolution, digest.k0
    return max(0, cell_floor(lo, res) - k0), min(digest.n, cell_ceil(hi, res) - k0)


def _fold(digest, channels, queries):
    """Append one day's usable cell maxima to each lo-ordered query it overlaps."""
    high = (digest.k0 + digest.n) * digest.resolution
    for _i, (lo, hi), columns in queries:
        if lo >= high:
            return
        a, b = _bounds(digest, lo, hi)
        for c in channels if a < b else ():
            columns[c] += digest.channels[c][a:b].translate(None, _UNUSABLE)


def _cells(digest, channels, spans):
    """One day's raw cells over ``spans``: ``[(first cell time, {channel: bytes})]``."""
    found = []
    for lo, hi in spans:
        a, b = _bounds(digest, lo, hi)
        if a < b:
            start = (digest.k0 + a) * digest.resolution
            found.append((start, {c: digest.channels[c][a:b] for c in channels}))
    return found


def _quantile(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))] if values else None


def _kept(channels, human, automatic):
    """Trusted ``(piece, reading)`` pairs: every human one, and automatic ones whose still
    reading looks empty against the empty pieces' own floor."""
    still = [c for c in channels if c % 2]
    floor = {
        c: _quantile([r[c][1] for _p, r in human + automatic if c in r], FLOOR_QUANTILE)
        for c in still
    }

    def occupied(read):
        return any(
            c in read and floor[c] is not None and read[c][1] >= floor[c] + OCCUPIED_ABOVE_FLOOR
            for c in still
        )

    return human + [(p, r) for p, r in automatic if not occupied(r)]


def _noise(channels, kept, human):
    """Per channel: the empty room's noise top. A human said their pieces were empty (a
    dog included), so their own quantile is a floor however rare they are among the rest."""
    found = {}
    for c in channels:
        tops = [
            _quantile([r[c][0] for _p, r in pieces if c in r], NOISE_QUANTILE)
            for pieces in (kept, human)
        ]
        tops = [t for t in tops if t is not None]
        found[c] = max(tops) if tops else None
    return found


def _allowed(value, domain):
    return next((t for t in domain if t >= value), domain[-1])


def _bursts(moves, noise, reads):
    """Per move channel: the readings' maxima above its noise."""
    return {
        c: [r[c][0] for r in reads if c in r and noise[c] is not None and r[c][0] > noise[c]]
        for c in moves
    }


def _arrived(windows, reads, kept_ends):
    """Readings of the arrival windows that follow trusted empty time."""
    found = []
    for (lo, _hi), read in zip(windows, reads, strict=True):
        start = lo + ARRIVAL_WINDOW
        i = bisect_right(kept_ends, start) - 1
        if i >= 0 and start - kept_ends[i] <= ARRIVAL_AFTER_EMPTY:
            found.append(read)
    return found


def _presence_reads(segments, moves):
    """Without any arrival, labelled presence's own move readings stand in for the burst:
    one reading per half-hour of presence cells."""
    step = int(PIECE_SECONDS // CELL_SECONDS)
    found = []
    for _start, columns in segments:
        for a in range(0, len(columns[moves[0]]), step):
            chunk = {c: columns[c][a : a + step].translate(None, _UNUSABLE) for c in moves}
            found.append(_stats(chunk))
    return found


def _placed(noise, arrival, domain):
    if arrival is None:
        return domain[-1]
    return _allowed(noise + GAP_SHARE * (arrival - noise), domain)


@dataclass(frozen=True)
class MovePlan:
    """Per radar: move thresholds from the gap, and the presence only move sees (where they
    detect while every still gate reads at most its empty-room noise), extended by the hold."""

    thresholds: dict
    coverage: dict


def _radar(problem, radar_id):
    """``(move thresholds, move-only coverage)`` of one radar; empty without trusted empty."""
    radar = problem.radars[radar_id]
    zones = [z for z in problem.zones if radar_id in z.members]
    moves = [c for c in radar.channels if c % 2 == 0]
    human, automatic = _radar_empty(zones)
    if not moves or not human + automatic:
        return {}, []
    human, automatic = _pieces(_inner(human)), _pieces(_inner(automatic))
    windows = [(a - ARRIVAL_WINDOW, a + ARRIVAL_WINDOW) for a in _arrivals(zones)]
    present = _merged((s.start, s.end) for z in zones for s in z.truth if s.state == "present")
    queries = human + automatic + windows
    reads, segments = _read_all(problem, radar_id, radar.channels, queries, present)
    split = len(human) + len(automatic)
    read = list(zip(human + automatic, reads[:split], strict=True))
    kept = _kept(radar.channels, read[: len(human)], read[len(human) :])
    if not kept:
        return {}, []
    noise = _noise(radar.channels, kept, read[: len(human)])
    arrived = _arrived(windows, reads[split:], sorted(b for (_a, b), _r in kept))
    bursts = _bursts(moves, noise, arrived or _presence_reads(segments, moves))
    thresholds = {
        c: _placed(noise[c], _quantile(bursts[c], 0.5), radar.domains[c])
        for c in moves
        if noise[c] is not None
    }
    stills = {c: noise[c] for c in radar.channels if c % 2 and noise[c] is not None}
    hold = _timing(radar.timing)[0]
    covered = (i for seg in segments for i in _detected(seg, thresholds, stills, hold))
    return thresholds, _merged(covered)


def move_plan(problem):
    """The move rule's plan for every radar of ``problem`` with trusted empty evidence."""
    thresholds, coverage = {}, {}
    for radar_id in problem.radars:
        moves, covered = _radar(problem, radar_id)
        if moves:
            thresholds[radar_id], coverage[radar_id] = moves, covered
    return MovePlan(thresholds, coverage)


def move_thresholds(problem):
    """``{radar_id: {move channel: threshold}}`` for radars with trusted empty evidence."""
    return move_plan(problem).thresholds


def _mask(columns, levels):
    """Cells (as an int bit mask) where some channel's maximum exceeds its level."""
    mask = 0
    for c, level in levels.items():
        mask |= int.from_bytes(columns[c].translate(_ABOVE[int(level)]), "little")
    return mask


def _detected(segment, thresholds, still_noise, hold):
    """A segment's intervals where move detects while every still gate reads its noise."""
    start, columns = segment
    size = len(next(iter(columns.values())))
    only_move = _mask(columns, thresholds) & ~_mask(columns, still_noise)
    cells = only_move.to_bytes(size, "little")
    return [
        (start + m.start() * CELL_SECONDS, start + m.end() * CELL_SECONDS + hold)
        for m in finditer(_RUN, cells)
    ]


def still_truth(problem, plan):
    """``problem`` whose presence labels leave out presence only move sees: asking still to
    catch it would only drag still into its noise. Empty labels stay whole."""
    zones = []
    for zone in problem.zones:
        covered = _merged(i for r in zone.members for i in plan.coverage.get(r, ()))
        truth = []
        for span in zone.truth:
            if span.state != "present" or not covered:
                truth.append(span)
                continue
            truth += [
                replace(span, start=a, end=b)
                for a, b in _subtract([(span.start, span.end)], covered)
            ]
        zones.append(replace(zone, truth=tuple(truth)))
    return replace(problem, zones=tuple(zones))

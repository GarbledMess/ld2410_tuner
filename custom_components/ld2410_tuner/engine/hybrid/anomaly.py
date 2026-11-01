"""Label audit: sub-stretches inside training labels that contradict their own class.

One mechanism for every source and both directions (a forgotten Present label, a
phone left in a room, presence inside a Not present window are only examples). It
reads only the fit's pure training cells and the fit's own labels.

1. Trimmed references per member radar and selected channel, from cells of windows
   not yet flagged: ``empty_bound`` = min(E90, E75 + 2 IQR + 1), ``empty_high`` =
   min(E99, E75 + 4 IQR + 2) of the empty class, and the present P10/P25/P50.
   ``ITERATIONS`` rounds re-estimate them without the windows the last round flagged.
2. ``WINDOW_SECONDS`` windows cut each label's training cells in time order (a short
   tail joins the previous window). A present window is empty-like when on EVERY
   selected channel of EVERY member radar at most ``EMPTY_LIKE_MAX`` of its cells
   exceed ``empty_bound``; an empty window is occupied-like when on SOME channel
   whose present median clears ``empty_high`` at least ``OCCUPIED_LIKE_MIN`` of its
   cells exceed max(``empty_high``, present P25 for human labels, P10 otherwise).
3. Flagged windows merge into stretches while the flagged share stays at least
   ``MIN_FLAGGED_SHARE`` (coverage breaks always split, up to ``EDGE_WINDOWS``
   unflagged edge windows join, sub-window remnants at a label edge go with the
   stretch). A suspect needs ``MIN_WINDOWS[state, source]`` flagged windows. The
   unit is a stretch inside a label, never a whole label for a bad part.
4. Caps: per class at most ``CAP_CLASS`` of its training time and ``CAP_HUMAN`` of
   its human time; non-human suspects first, then longer ones. A class never
   vanishes.

Whether suspects are discarded is the fit's decision (``engine``): masked time is
unobserved for fitter and judge, never flipped, and labels keep their episodes.
"""

from collections import Counter
from dataclasses import dataclass, field, replace
from itertools import compress, repeat
from math import ceil
from operator import ge, itemgetter, le, mul, sub

from .candidates import preferred, quantile
from .constants import (
    ANOMALY_ROWS,
    AUDIT_BUFFER_BYTES,
    CAP_CLASS,
    CAP_HUMAN,
    CELL_SECONDS,
    CHANNELS,
    EDGE_WINDOWS,
    EMPTY_LIKE_MAX,
    EMPTY_LIKE_Q,
    ITERATIONS,
    MIN_FLAGGED_SHARE,
    MIN_WINDOWS,
    OCCUPIED_LIKE_MIN,
    OCCUPIED_LIKE_Q,
    REFIT_OPERATIONS,
    WINDOW_SECONDS,
)
from .domains import evidence_margin
from .evidence import _labels
from .replay import TABLES
from .scoring import _union, masked
from .search import channel_views, class_bins, repair

STATES = ("present", "not_present")
PRESENT_LEVEL = {"human": "p25", "entity": "p10"}
REASON = {
    "present": "indistinguishable from empty on every selected gate",
    "not_present": "sustained energy above the empty class on a selected gate",
}
_ROWS = "SELECT state,source,vector,t,n FROM search_pure WHERE zone=? ORDER BY t"


@dataclass
class AnomalyAudit:
    accepted: list = field(default_factory=list)
    capped: list = field(default_factory=list)
    masks: dict = field(default_factory=dict)


def _kind(source):
    return "human" if source == "human" else "entity"


def _training_labels(problem, zone):
    out = []
    for lo, hi, span in _labels(zone.truth):
        lo, hi = max(lo, problem.start), min(hi, problem.end)
        if hi > lo and span.state in STATES:
            out.append((lo, hi, span))
    return out


def windows_of(spans, cells):
    """Per label index: ordered windows ``(side, lo, hi, t_lo, t_hi)`` in buffer cells."""
    out = {}
    for side, label, lo, hi, t0 in spans:
        wins = out.setdefault(label, [])
        a = lo
        while a < hi:
            b = min(hi, a + cells)
            if b - a < cells // 2 and a > lo:
                s, plo, _, pt, _ = wins[-1]
                wins[-1] = (s, plo, b, pt, t0 + (b - lo) * CELL_SECONDS)
            else:
                wins.append(
                    (side, a, b, t0 + (a - lo) * CELL_SECONDS, t0 + (b - lo) * CELL_SECONDS)
                )
            a = b
    return out


class _ZoneCells:
    """A zone's pure training cells in label order: one buffer per class side.

    Joined member vectors are buffered for at most ``AUDIT_BUFFER_BYTES``; wider
    rooms are read in member groups with identical results and order.
    """

    def __init__(self, problem, store, zone):
        self.store, self.zone = store, zone
        self.members = tuple(sorted(zone.members))
        self.labels = _training_labels(problem, zone)
        self.spans, self.sides = [], [0, 0]
        self._group = None
        self._scan(None)
        cells = max(1, sum(self.sides))
        size = max(1, AUDIT_BUFFER_BYTES // (CHANNELS * cells))
        self.groups = [self.members[i : i + size] for i in range(0, len(self.members), size)]

    def _runs(self):
        """Pure runs in time order with their label index and class side."""
        label = 0
        for state, _source, vector, t, n in self.store._database.execute(
            _ROWS, (self.zone.group_id,)
        ):
            while label < len(self.labels) and self.labels[label][1] <= t:
                label += 1
            yield 0 if state == "present" else 1, label, vector, t, n

    def _scan(self, group):
        """Assign pure runs to labels; with ``group``, also buffer its vectors."""
        # Groups are consecutive slices of the sorted members, so one slice per run.
        positions = [self.members.index(m) for m in group or ()]
        lo = positions[0] * CHANNELS if positions else 0
        hi = (positions[-1] + 1) * CHANNELS if positions else 0
        buffers = [bytearray(), bytearray()]
        spans, sides, previous = [], [0, 0], None
        for side, label, vector, t, n in self._runs():
            if positions:
                buffers[side] += vector[lo:hi] * n
            if previous == (label, t):
                spans[-1][3] += n
            else:
                spans.append([side, label, sides[side], sides[side] + n, t])
            sides[side] += n
            previous = label, t + n * CELL_SECONDS
        if group is None:
            self.spans = [tuple(s) for s in spans]
            self.sides = sides
        return buffers

    def column(self, key, side):
        rid, channel = key
        group = next(g for g in self.groups if rid in g)
        if self._group is None or self._group[0] != group:
            self._group = group, self._scan(group)
        width = CHANNELS * len(group)
        return bytes(self._group[1][side][group.index(rid) * CHANNELS + channel :: width])

    def human_seconds(self):
        human = dict.fromkeys(STATES, 0.0)
        for lo, hi, span in self.labels:
            if span.source == "human":
                human[span.state] += hi - lo
        return human

    def training_seconds(self):
        return {state: self.sides[i] * CELL_SECONDS for i, state in enumerate(STATES)}


def _histogram(column):
    """Cell counts per energy 0..100 (counting each present value in C)."""
    bins = [0] * 101
    for value in set(column):
        if value <= 100:
            bins[value] = column.count(value)
    return bins


def _bounds(bp, be):
    if not sum(be) or not sum(bp):
        return None
    e25, e75 = quantile(be, 0.25), quantile(be, 0.75)
    iqr = e75 - e25
    return {
        "empty_bound": min(quantile(be, EMPTY_LIKE_Q), e75 + 2 * iqr + 1),
        "empty_high": min(quantile(be, OCCUPIED_LIKE_Q), e75 + 4 * iqr + 2),
        "p10": quantile(bp, 0.10),
        "p25": quantile(bp, 0.25),
        "p50": quantile(bp, 0.5),
    }


def _above_counts(above, windows):
    """Cells above the level per window, counted by C calls over the window list."""
    return map(above.count, repeat(1), map(itemgetter(1), windows), map(itemgetter(2), windows))


def _limits(share, windows):
    return map(
        mul, repeat(share), map(sub, map(itemgetter(2), windows), map(itemgetter(1), windows))
    )


def _empty_step(active, r, values):
    """Keep present windows with at most ``EMPTY_LIKE_MAX`` of cells above the bound."""
    if not active or r is None:
        return active
    above = values.translate(TABLES[min(100, r["empty_bound"])])
    keep = map(le, _above_counts(above, active), _limits(EMPTY_LIKE_MAX, active))
    return list(compress(active, keep))


def empty_like(windows, ref, column):
    """Present windows with at most ``EMPTY_LIKE_MAX`` of cells above the empty bound
    on EVERY selected channel of EVERY member radar; rejected windows stay rejected."""
    active = list(windows)
    for key, r in ref.items():
        if not active:
            break
        if r is not None:
            active = _empty_step(active, r, column(key, 0))
    return active


def _by_kind(windows, source_of):
    """Unflagged empty windows per label kind, each list in window order."""
    return {kind: [w for w in windows if source_of[w] == kind] for kind in ("human", "entity")}


def _occupied_step(active, r, values, flagged):
    """Flag ``active`` (per kind) windows with sustained presence-level energy here.

    Flagged windows leave their kind's list; returns whether any window remains.
    """
    if r is None or r["p50"] <= r["empty_high"]:
        return any(active.values())
    for kind, selected in active.items():
        if not selected:
            continue
        level = min(100, max(r["empty_high"], r[PRESENT_LEVEL[kind]]))
        above = values.translate(TABLES[level])
        hit = map(ge, _above_counts(above, selected), _limits(OCCUPIED_LIKE_MIN, selected))
        found = set(compress(selected, hit))
        if found:
            flagged |= found
            active[kind] = [w for w in selected if w not in found]
    return any(active.values())


def occupied_like(windows, ref, column, source_of):
    """Empty windows with sustained presence-level energy on SOME qualifying channel."""
    flagged, active = set(), _by_kind(windows, source_of)
    for key, r in ref.items():
        if not any(active.values()):
            break
        if r is not None and r["p50"] > r["empty_high"]:
            _occupied_step(active, r, column(key, 1), flagged)
    return [w for w in windows if w in flagged]


class _Audit:
    def __init__(self, problem, store, zone):
        self.problem, self.zone = problem, zone
        self.cells = _ZoneCells(problem, store, zone)
        self.keys = [
            (rid, c) for rid in self.cells.members for c in sorted(problem.radars[rid].channels)
        ]
        self.windows = windows_of(self.cells.spans, max(1, WINDOW_SECONDS // CELL_SECONDS))
        self.source_of = {
            w: _kind(self.cells.labels[label][2].source)
            for label, wins in self.windows.items()
            for w in wins
        }

    def run(self):
        present = [w for wins in self.windows.values() for w in wins if w[0] == 0]
        empty = [w for wins in self.windows.values() for w in wins if w[0] == 1]
        full, flagged = {}, set()
        for _ in range(ITERATIONS):
            new = self._iteration(full, flagged, present, empty)
            if new == flagged:
                break
            flagged = new
        return self._stretches(flagged)

    def _iteration(self, full, flagged, present, empty):
        """One reference round; each key's columns are read once, member group by group.

        Every key's reference depends only on its own columns, and the empty-like
        (every key) and occupied-like (some key) tests are order-free filters, so
        one pass equals separate reference, empty and occupied passes.
        """
        active, remaining, hit = list(present), _by_kind(empty, self.source_of), set()
        for key in self.keys:
            columns = tuple(self.cells.column(key, side) for side in (0, 1))
            if key not in full:
                full[key] = tuple(_histogram(column) for column in columns)
            r = _reference(full[key], columns, flagged)
            active = _empty_step(active, r, columns[0])
            _occupied_step(remaining, r, columns[1], hit)
        return set(active) | hit

    def _stretches(self, flagged):
        out = []
        for label, wins in self.windows.items():
            lo, hi, span = self.cells.labels[label]
            need = MIN_WINDOWS[span.state, _kind(span.source)]
            for group in _groups(wins, flagged):
                n = sum(1 for w in group if w in flagged)
                if n >= need and n >= MIN_FLAGGED_SHARE * len(group):
                    out.append(self._stretch(group, n, lo, hi, span))
        out.sort(key=lambda s: s["start"])
        return out

    def _stretch(self, group, flagged, lo, hi, span):
        start, end = group[0][3], group[-1][4]
        start = lo if start - lo < WINDOW_SECONDS else start
        end = hi if hi - end < WINDOW_SECONDS else end
        return {
            "zone": self.zone.group_id,
            "start": start,
            "end": end,
            "state": span.state,
            "source": span.source,
            "seconds": end - start,
            "windows": len(group),
            "flagged_windows": flagged,
            "reason": REASON[span.state],
        }


def _dropped(columns, flagged):
    counts = (Counter(), Counter())
    for side, lo, hi, _, _ in flagged:
        counts[side].update(columns[side][lo:hi])
    return counts


def _reference(full, columns, flagged):
    """Trimmed bounds of one key without the flagged windows' cells."""
    drop = _dropped(columns, flagged) if flagged else ({}, {})
    sides = []
    for side, bins in enumerate(full):
        kept = [m - drop[side].get(v, 0) for v, m in enumerate(bins)]
        sides.append(kept if sum(kept) else list(bins))
    return _bounds(*sides)


def _blocks(windows):
    blocks, block = [], []
    for w in windows:
        if block and block[-1][4] != w[3]:
            blocks.append(block)
            block = []
        block.append(w)
    if block:
        blocks.append(block)
    return blocks


def _runs(block, flagged):
    segs = []
    for i, w in enumerate(block):
        if w in flagged:
            if segs and segs[-1][1] == i - 1:
                segs[-1][1] = i
                segs[-1][2] += 1
            else:
                segs.append([i, i, 1])
    return segs


def _merge(segs):
    """Merge neighbouring runs, best flagged share first, while the share holds."""
    while len(segs) > 1:
        best = None
        for k in range(len(segs) - 1):
            share = (segs[k][2] + segs[k + 1][2]) / (segs[k + 1][1] - segs[k][0] + 1)
            if share >= MIN_FLAGGED_SHARE and (best is None or share > best[0]):
                best = share, k
        if best is None:
            break
        k = best[1]
        segs[k : k + 2] = [[segs[k][0], segs[k + 1][1], segs[k][2] + segs[k + 1][2]]]
    return segs


def _edges(first, end, n, last):
    """Let up to ``EDGE_WINDOWS`` unflagged windows at a block edge join a stretch."""
    if 0 < first <= EDGE_WINDOWS and n >= MIN_FLAGGED_SHARE * (end + 1):
        first = 0
    if 0 < last - end <= EDGE_WINDOWS and n >= MIN_FLAGGED_SHARE * (last - first + 1):
        end = last
    return first, end


def _groups(windows, flagged):
    """Stretches of time-contiguous windows around flagged runs (breaks always split)."""
    for block in _blocks(windows):
        for first, end, n in _merge(_runs(block, flagged)):
            first, end = _edges(first, end, n, len(block) - 1)
            yield block[first : end + 1]


def limit(suspects, training, human):
    """Apply the caps; returns (accepted, capped). Non-human first, then longer first."""
    used, used_human = {}, {}
    accepted, capped = [], []
    order = sorted(suspects, key=lambda s: (s["source"] == "human", -s["seconds"], s["start"]))
    for s in order:
        state, seconds = s["state"], s["seconds"]
        if used.get(state, 0.0) + seconds > CAP_CLASS * training.get(state, 0.0):
            capped.append(s)
            continue
        if s["source"] == "human" and (
            used_human.get(state, 0.0) + seconds > CAP_HUMAN * human.get(state, 0.0)
        ):
            capped.append(s)
            continue
        used[state] = used.get(state, 0.0) + seconds
        if s["source"] == "human":
            used_human[state] = used_human.get(state, 0.0) + seconds
        accepted.append(s)
    accepted.sort(key=lambda s: (s["zone"], s["start"]))
    return accepted, capped


def audit(problem, training):
    """Capped suspect sub-stretches of every zone and their per-zone masks."""
    found = AnomalyAudit()
    for zone in sorted(problem.zones, key=lambda z: z.group_id):
        if not zone.members:
            continue
        audited = _Audit(problem, training, zone)
        accepted, capped = limit(
            audited.run(), audited.cells.training_seconds(), audited.cells.human_seconds()
        )
        found.accepted += accepted
        found.capped += capped
    for s in found.accepted:
        found.masks.setdefault(s["zone"], []).append((s["start"], s["end"]))
    found.masks = {gid: _union(v) for gid, v in found.masks.items()}
    return found


def masked_problem(problem, masks):
    """The same problem with audited time unobserved in each zone."""
    zones = tuple(masked(z, masks.get(z.group_id, ())) for z in problem.zones)
    return replace(problem, zones=zones)


def _centre(problem, start, stats):
    return {
        rid: {c: preferred(radar.domains[c], start[rid][c], stats[rid, c]) for c in radar.channels}
        for rid, radar in problem.radars.items()
    }


def masked_seeds(problem, training, masks, start, *, quiet):
    """Closed-form masked gap centres (no search) and the masked statistics."""
    bins = class_bins(problem, training, masks)
    stats = channel_views(bins)
    seeds = [repair(_centre(problem, start, stats), stats, start)]
    if quiet:
        attenuated = channel_views(bins, attenuate=True)
        seeds.append(repair(_centre(problem, start, attenuated), stats, start))
    return seeds, stats


def operations(problem, plan, patterns):
    """Deterministic row-pattern work of a masked re-score at fit 1's fidelity.

    Baseline and quiet passes both count. A cell pre-rank charges every pattern on
    six-second cells and only the bounded exact finalists on physical rows.
    """
    cells = sum(ceil((b - a) / CELL_SECONDS) for a, b in plan.regions) * len(problem.radars)
    if plan.mode == "exact":
        return plan.row_count * patterns * 2
    work = cells * patterns * 2
    if plan.mode == "cells_then_exact":
        work += plan.row_count * min(patterns, max(2, plan.finalist_limit)) * 2
    return work


def within_budget(work):
    return work <= REFIT_OPERATIONS


def masked_margin(start):
    """Evidence band of the masked training labels, summed over zones."""
    return sum(
        evidence_margin(z["occupied_episodes"] + z["empty_periods"], 0)
        for z in start["zones"].values()
    )


def _row(s, decision):
    return {**s, "decision": decision}


def _by_source(stretches):
    out = {}
    for s in stretches:
        item = out.setdefault(f"{s['state']}/{s['source']}", {"seconds": 0.0, "stretches": 0})
        item["seconds"] += s["seconds"]
        item["stretches"] += 1
    return out


def report(found, status, *, gain=None, margin=None, work=None):
    """The additive ``anomaly`` report: decision, totals and at most 50 rows each."""
    discarded = found.accepted if status == "discarded" else []
    kept = found.accepted if status not in ("discarded", "clean") else []
    return {
        "status": status,
        "suspects": len(found.accepted) + len(found.capped),
        "gain": gain,
        "margin": margin,
        "operations": work,
        "operation_budget": REFIT_OPERATIONS,
        "discarded_count": len(discarded),
        "discarded_seconds": _by_source(discarded),
        "discarded": [_row(s, "discarded") for s in discarded][:ANOMALY_ROWS],
        "kept": [_row(s, status) for s in kept][:ANOMALY_ROWS],
        "capped": [_row(s, "kept: cap") for s in found.capped][:ANOMALY_ROWS],
    }

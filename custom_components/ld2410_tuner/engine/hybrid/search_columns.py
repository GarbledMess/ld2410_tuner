"""Columnar decisions for zones too large for one packed block.

A zone's pure training cells are compiled once per search into byte columns (one
per member channel) within strata of one parent episode and confidence, so every
row of a stratum carries the same mass. A coordinate's crossing distribution is
then a few C translations: rows another lane already detects fold into the
always-detected bin, the rest are counted by their coordinate energy. Both the
conditional histogram tails and the per-episode coordinate statistics follow
exactly; no cell, episode or multiplicity is dropped. ``COLUMN_BYTES`` bounds
the resident columns; larger zones keep the streaming path.
"""

from collections import Counter
from itertools import accumulate, groupby

from ...const import HISTORY_KEYS
from .constants import CELL_SECONDS, CHANNELS
from .replay import TABLES

COLUMN_BYTES = 32 * 1024**2
_DETECTED = 255
_VALID = bytes(value if value <= 100 else 0 for value in range(256))
_ATTENUATED = bytes(3 * value // 5 if value <= 100 else 0 for value in range(256))
_TOTALS = (
    "SELECT state,source,parent,confidence,sum(n) FROM search_groups WHERE zone=? "
    "GROUP BY state,source,parent,confidence ORDER BY state,source,parent,confidence"
)
_ROWS = (
    "SELECT source,parent,confidence,n,vector FROM search_groups "
    "WHERE zone=? AND state=? AND (source='human')=? "
    "ORDER BY state,source,parent,confidence,vector"
)


def _kind(source):
    return "human" if source == "human" else "automatic"


def _unpriced(statistics, policy):
    """Lanes whose errors cost nothing, so no threshold can change their contribution."""
    from .search_scores import _coefficients

    priced = {key for key, _, value in _coefficients(statistics, policy) if value}
    return {key: stats for key, stats in statistics.items() if key not in priced}


def _lane(state, source):
    return state + "/" + _kind(source)


def _episode_totals(strata):
    """Seconds and weighted seconds of each parent, adding its strata in order."""
    for key, rows in groupby(strata, key=lambda row: row[:3]):
        seconds = weighted = 0.0
        for *_, confidence, count in rows:
            mass = count * CELL_SECONDS
            seconds += mass
            weighted += mass * confidence
        yield key, seconds, weighted


def _lane_totals(strata):
    totals = {}
    for state, source, _parent, confidence, count in strata:
        lane = _lane(state, source)
        totals[lane] = totals.get(lane, 0.0) + confidence * CELL_SECONDS * count
    return totals


class _Section:
    """One lane's rows: energy columns plus each stratum's range within them."""

    def __init__(self, rows, tables):
        width = len(tables)
        joined, self.strata = bytearray(), []
        for key, group in groupby(rows, key=lambda row: row[:3]):
            start = len(joined) // width
            for *_, count, vector in group:
                joined.extend(vector * count)
            self.strata.append((key, start, len(joined) // width))
        self.size = len(joined) // width
        self.columns = [bytes(joined[at::width]).translate(t) for at, t in enumerate(tables)]

    def crossings(self, lanes, position, state):
        fixed = _fixed(self.columns, lanes)
        if position is None:
            yield from self._outside(fixed, state)
            return
        column = self.columns[position]
        if fixed:
            flags = fixed * _DETECTED
            value = (int.from_bytes(column, "little") & ~flags) | flags
            column = value.to_bytes(self.size, "little")
        for (source, parent, confidence), start, stop in self.strata:
            counts = Counter(column[start:stop])
            detected = counts.pop(_DETECTED, 0)
            if detected:
                counts[101] = detected
            yield (state, source, parent), confidence, stop - start, counts

    def _outside(self, fixed, state):
        flags = fixed.to_bytes(self.size, "little")
        for (source, parent, confidence), start, stop in self.strata:
            detected = flags.count(1, start, stop)
            counts = {101: detected} if detected else {}
            counts[-1] = stop - start - detected
            yield (state, source, parent), confidence, stop - start, counts


class ColumnBlock:
    """Strata ``(state, source, parent, confidence)`` of transformed energy columns.

    Readings are compiled only for lanes whose errors have a price; a lane without one
    (automatic evidence at zero weight) contributes its fixed masses and nothing else.
    """

    def __init__(self, problem, store, zone, attenuate):
        self.members = tuple(sorted(zone.members))
        self.width = CHANNELS * len(self.members)
        from .search_scores import _settle, zone_policy

        strata = store._database.execute(_TOTALS, (zone.group_id,)).fetchall()
        policy = zone_policy(problem, zone)
        statistics = [{}]
        for key, seconds, weighted in _episode_totals(strata):
            _settle(statistics, key, seconds, weighted, (0.0,), (0.0,))
        self.settled = _unpriced(statistics[0], policy)
        self.totals = _lane_totals(strata)
        self.free_tails = _unpriced(
            {lane: [total, 0.0, 0.0, 0.0] for lane, total in self.totals.items()}, policy
        )
        self.sections = {}
        for state, kind in sorted({(row[0], _kind(row[1])) for row in strata}):
            lane = state + "/" + kind
            if lane not in self.settled or lane not in self.free_tails:
                self.sections[lane] = self._section(problem, store, zone, state, kind, attenuate)

    def _section(self, problem, store, zone, state, kind, attenuate):
        rows = store._database.execute(_ROWS, (zone.group_id, state, kind == "human"))
        present = state == "present"
        final = _ATTENUATED if present and attenuate else _VALID
        supported = present and kind != "human"
        tables = [
            self._table(problem, at, final) if supported else final for at in range(self.width)
        ]
        return _Section(rows, tables)

    def _table(self, problem, at, final):
        member, channel = self.members[at // CHANNELS], at % CHANNELS
        model = problem.policy.quality.get("models", {}).get(member)
        if model is None:
            return final
        key = HISTORY_KEYS[channel]
        support = bytes(v if v <= 100 and model.supports(key, v) else 0 for v in range(256))
        return support.translate(final)

    def _crossings(self, pattern, rid, channel, skipped):
        """Per stratum: ``(key, confidence, rows, {crossing: rows})``.

        Crossing is the coordinate energy, 101 when another lane already detects
        the row, and -1 when the coordinate's radar is not a member.
        """
        thresholds = [pattern[m].get(c, 100) for m in self.members for c in range(CHANNELS)]
        position = self.members.index(rid) * CHANNELS + channel if rid in self.members else None
        lanes = [(at, t) for at, t in enumerate(thresholds) if at != position and t < 100]
        for lane, section in self.sections.items():
            if lane not in skipped:
                yield from section.crossings(lanes, position, lane.split("/")[0])

    def lanes(self, pattern, rid, channel):
        """``{lane: [mass, bins]}`` with ``bins[crossing + 1]`` as the streaming path."""
        lanes = {lane: [total, [0.0] * 103] for lane, total in self.totals.items()}
        found = self._crossings(pattern, rid, channel, self.free_tails)
        for key, confidence, _rows, counts in found:
            mass = confidence * CELL_SECONDS
            bins = lanes[_lane(key[0], key[1])][1]
            for crossing, count in counts.items():
                bins[crossing + 1] += mass * count
        return lanes

    def episodes(self, pattern, rid, channel, candidates):
        """Per priced parent episode: seconds, weighted seconds and seen per candidate."""
        out, current = [], None
        for key, confidence, rows, counts in self._crossings(pattern, rid, channel, self.settled):
            if current is None or current[0] != key:
                current = [key, 0.0, 0.0, [0.0] * len(candidates), [0.0] * len(candidates)]
                out.append(current)
            seconds = rows * CELL_SECONDS
            current[1] += seconds
            current[2] += seconds * confidence
            above = _above(counts, candidates)
            for i, count in enumerate(above):
                current[3][i] += count * CELL_SECONDS
                current[4][i] += count * CELL_SECONDS * confidence
        return [(key, s, w, tuple(seen), tuple(wseen)) for key, s, w, seen, wseen in out]


def _fixed(columns, lanes):
    """Rows (byte lanes) that some other configured channel already detects."""
    fixed = 0
    for at, t in lanes:
        fixed |= int.from_bytes(columns[at].translate(TABLES[t]), "little")
    return fixed


def _above(counts, candidates):
    """Rows whose crossing exceeds each sorted candidate threshold."""
    bins = [0] * 103
    for crossing, count in counts.items():
        bins[crossing + 1] += count
    tail = list(accumulate(reversed(bins)))[::-1]  # tail[k] = rows with crossing >= k - 1
    return [tail[t + 2] if t + 2 < len(tail) else 0 for t in candidates]


def column_block(problem, store, zone, attenuate):
    """The zone's compiled block, or None beyond the byte budget (streaming path).

    Blocks of every zone stay cached for the search while their total bytes fit.
    """
    blocks = store.__dict__.setdefault("_column_blocks", {})
    key = zone.group_id, attenuate, id(problem.policy)
    if key in blocks:
        return blocks[key][0]
    cells = store._database.execute(
        "SELECT coalesce(sum(n),0) FROM search_groups WHERE zone=?", (zone.group_id,)
    ).fetchone()[0]
    size = cells * CHANNELS * len(zone.members)
    used = sum(bytes_ for _, bytes_ in blocks.values())
    block = None
    if cells and used + size <= COLUMN_BYTES:
        block = ColumnBlock(problem, store, zone, attenuate)
    blocks[key] = block, size if block is not None else 0
    return block

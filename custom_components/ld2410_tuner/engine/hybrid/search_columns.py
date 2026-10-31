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

from itertools import accumulate

from ...const import HISTORY_KEYS
from .constants import CELL_SECONDS, CHANNELS
from .replay import TABLES

COLUMN_BYTES = 32 * 1024**2
_VALID = bytes(value if value <= 100 else 0 for value in range(256))
_ATTENUATED = bytes(3 * value // 5 if value <= 100 else 0 for value in range(256))
_ROWS = (
    "SELECT state,source,parent,confidence,n,vector FROM search_groups WHERE zone=? "
    "ORDER BY state,source,parent,confidence,vector"
)


def _kind(source):
    return "human" if source == "human" else "automatic"


def _support(problem, member, channel):
    model = problem.policy.quality.get("models", {}).get(member)
    if model is None:
        return None
    key = HISTORY_KEYS[channel]
    return bytes(v if v <= 100 and model.supports(key, v) else 0 for v in range(256))


class ColumnBlock:
    """Strata ``(state, source, parent, confidence)`` of transformed energy columns."""

    def __init__(self, problem, store, zone, attenuate):
        self.members = tuple(sorted(zone.members))
        self.width = CHANNELS * len(self.members)
        self.strata = []
        pending = {}
        for state, source, parent, confidence, count, vector in store._database.execute(
            _ROWS, (zone.group_id,)
        ):
            pending.setdefault((state, source, parent, confidence), bytearray()).extend(
                vector * count
            )
        for (state, source, parent, confidence), joined in pending.items():
            columns = self._columns(problem, joined, state, _kind(source), attenuate)
            rows = len(joined) // self.width
            self.strata.append(((state, source, parent), confidence, rows, columns))

    def _columns(self, problem, joined, state, kind, attenuate):
        present = state == "present"
        final = _ATTENUATED if present and attenuate else _VALID
        out = []
        for at in range(self.width):
            member, channel = self.members[at // CHANNELS], at % CHANNELS
            column = bytes(joined[at :: self.width])
            support = _support(problem, member, channel) if present and kind != "human" else None
            if support is not None:
                column = column.translate(support)
            out.append(column.translate(final))
        return out

    def _crossings(self, pattern, rid, channel):
        """Per stratum: ``(key, confidence, rows, {crossing: rows})``.

        Crossing is the coordinate energy, 101 when another lane already detects
        the row, and -1 when the coordinate's radar is not a member.
        """
        thresholds = [pattern[m].get(c, 100) for m in self.members for c in range(CHANNELS)]
        position = self.members.index(rid) * CHANNELS + channel if rid in self.members else None
        lanes = [(at, t) for at, t in enumerate(thresholds) if at != position and t < 100]
        for key, confidence, rows, columns in self.strata:
            fixed = _fixed(columns, lanes)
            detected = fixed.bit_count()
            counts = {101: detected} if detected else {}
            if position is None:
                counts[-1] = rows - detected
            else:
                counts.update(_histogram(columns[position], fixed, rows))
            yield key, confidence, rows, counts

    def lanes(self, pattern, rid, channel):
        """``{lane: [mass, bins]}`` with ``bins[crossing + 1]`` as the streaming path."""
        lanes = {}
        for (state, source, _), confidence, rows, counts in self._crossings(pattern, rid, channel):
            mass = confidence * CELL_SECONDS
            lane = lanes.setdefault(state + "/" + _kind(source), [0.0, [0.0] * 103])
            lane[0] += mass * rows
            for crossing, count in counts.items():
                lane[1][crossing + 1] += mass * count
        return lanes

    def episodes(self, pattern, rid, channel, candidates):
        """Per parent episode: seconds, weighted seconds and seen per candidate."""
        out, current = [], None
        for key, confidence, rows, counts in self._crossings(pattern, rid, channel):
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


def _histogram(column, fixed, rows):
    """Counts of each coordinate energy among rows no other lane detects."""
    if fixed:
        flags = fixed * 255
        column = ((int.from_bytes(column, "little") & ~flags) | flags).to_bytes(rows, "little")
    for value in sorted(set(column)):
        if value <= 100:
            yield value, column.count(value)


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

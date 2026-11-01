"""Bounded six-second packed shift dilation, with gap barriers and hold carry."""

# Spill files are written and read only by this process, in its private temp directory.
import pickle  # nosec B403
import struct
import tempfile
from array import array
from bisect import bisect_right
from collections import OrderedDict
from math import ceil
from pathlib import Path

from ...calibration.event_replay import _timing
from ...const import HISTORY_KEYS
from .constants import CELL_SECONDS, SHARD_CELLS
from .evidence import _labels

_TABLES = tuple(bytes(49 if t < v <= 100 else 48 for v in range(256)) for t in range(101))
_COVER = bytes(49 if value == 1 else 48 for value in range(256))
_ATT = bytes(3 * v // 5 if v <= 100 else 255 for v in range(256))
_SEEN = struct.Struct("<dd")
# One projected block (label runs plus timing reach) replaces fixed physical blocks
# while it has at most this many cells; its columns spill per channel, raw.
PROJECTED_CELLS = 2**21
# Streamed parent counters and denominators stay resident up to this many parents.
RESIDENT_PARENTS = 2**16


def pack_mask(column, threshold):
    """Strict detection bits, earliest at bit zero; all parsing runs in C."""
    return int(column[::-1].translate(_TABLES[threshold]), 2) if column and threshold < 100 else 0


def _bridge(coverage, distance):
    result, span = coverage, 1
    while span * 2 <= distance:
        result &= result << span
        span *= 2
    if span < distance:
        result &= result << (distance - span)
    return result


def dilate(hits, cells, coverage):
    """Prototype forward doubling shifts, masked against physical gaps."""
    hits &= coverage
    span = 1
    while span * 2 <= cells + 1:
        hits |= (hits << span) & _bridge(coverage, span)
        span *= 2
    if span < cells + 1:
        distance = cells + 1 - span
        hits |= (hits << distance) & _bridge(coverage, distance)
    return hits & coverage


def _range(lo, hi):
    return ((1 << (hi - lo)) - 1) << lo if hi > lo else 0


def _present(zone, lo, size):
    result = 0
    for a, b, span in _labels(zone.truth):
        if span.state == "present":
            start = max(0, ceil((a - lo) / CELL_SECONDS))
            end = min(size, int((b - lo) // CELL_SECONDS))
            result |= _range(start, end)
    return result


def _columns(problem, rid, lo, size, channels):
    columns = {c: bytearray([255]) * size for c in channels}
    coverage = bytearray(size)
    for day in problem.days.dates(rid):
        digest = problem.days.digest(rid, day, channels)
        if digest is None:
            continue
        try:
            start = max(0, int(lo // CELL_SECONDS) - digest.k0)
            end = min(digest.n, int(lo // CELL_SECONDS) + size - digest.k0)
            if end <= start:
                continue
            at = digest.k0 + start - int(lo // CELL_SECONDS)
            coverage[at : at + end - start] = digest.cov[start:end]
            for c in channels:
                columns[c][at : at + end - start] = digest.channels[c][start:end]
        finally:
            close = getattr(digest.partial, "close", None)
            if close is not None:
                close()
    return {c: bytes(col) for c, col in columns.items()}, int(
        coverage[::-1].translate(_COVER), 2
    ) if coverage else 0


def _projected_columns(problem, rid, lo, channels, projection, size):
    """Projected byte columns and coverage, copied one radar-day at a time."""
    columns = {c: bytearray([255]) * size for c in channels}
    coverage = bytearray(size)
    offsets = [offset for offset, _bits, _position in projection]
    base = int(lo // CELL_SECONDS)
    for day in problem.days.dates(rid):
        digest = problem.days.digest(rid, day, channels)
        if digest is None:
            continue
        try:
            _copy_day(digest, columns, coverage, projection, offsets, base)
        finally:
            close = getattr(digest.partial, "close", None)
            if close is not None:
                close()
    return {c: bytes(col) for c, col in columns.items()}, int(
        coverage[::-1].translate(_COVER), 2
    ) if coverage else 0


def _copy_day(digest, columns, coverage, projection, offsets, base):
    first, last = digest.k0 - base, digest.k0 - base + digest.n
    at = max(0, bisect_right(offsets, first) - 1)
    for run in projection[at:]:
        if run[0] >= last:
            break
        _copy_run(digest, columns, coverage, run, first, last)


def _copy_run(digest, columns, coverage, run, first, last):
    offset, bits, position = run
    a, b = max(offset, first), min(offset + bits.bit_length(), last)
    if b <= a:
        return
    source, target = a - first, position + a - offset
    coverage[target : target + b - a] = digest.cov[source : source + b - a]
    for c, column in columns.items():
        column[target : target + b - a] = digest.channels[c][source : source + b - a]


def _advance(raw, coverage, hold, carry, size):
    missing = ((1 << size) - 1) ^ coverage
    prefix = (missing & -missing).bit_length() - 1 if missing else size
    active = dilate(raw, hold, coverage) | _range(0, min(carry, prefix))
    if not coverage & (1 << (size - 1)):
        return active, 0
    trailing = size - missing.bit_length()
    last = raw.bit_length()
    remainder = max(0, hold - (size - last)) if last > size - trailing else 0
    if prefix == size:
        remainder = max(remainder, carry - size)
    return active, remainder


def _project(mask, ranges):
    result = 0
    for offset, bits, position in ranges:
        result |= ((mask >> offset) & bits) << position
    return result


def _runs(bits, padding=0):
    result, position = [], 0
    while bits:
        offset = (bits & -bits).bit_length() - 1
        local = bits >> offset
        count = (local ^ (local + 1)).bit_length() - 1
        result.append((offset, (1 << count) - 1, position))
        position += count + padding
        bits &= ~(((1 << count) - 1) << offset)
    return tuple(result), position


def _local(bits):
    offset = (bits & -bits).bit_length() - 1 if bits else 0
    return offset, bits >> offset


def _add_bits(groups, key, offset, bits):
    if key in groups:
        old, mask = groups[key]
        start = min(old, offset)
        groups[key] = start, (mask << (old - start)) | (bits << (offset - start))
    else:
        groups[key] = offset, bits


_LABELS = (
    "SELECT p.state,p.source,p.parent,p.confidence,p.t,p.n FROM search_pure p "
    "WHERE p.zone=? AND p.t<? AND p.t+p.n*?>? ORDER BY p.t"
)
_LABELS_BY_PID = (
    "SELECT p.state,p.source,q.pid,p.confidence,p.t,p.n FROM search_pure p "
    "JOIN timing_parents q ON p.zone=q.zone AND p.state=q.state "
    "AND p.source=q.source AND p.parent IS q.parent "
    "WHERE p.zone=? AND p.t<? AND p.t+p.n*?>? ORDER BY p.t"
)


class Geometry:
    """Search-lifetime geometry with a bounded resident payload and disk overflow."""

    def __init__(self, problem, store, budget=16 * 1024**2):
        self.problem, self.store = problem, store
        self.budget = budget
        self.resident, self.locations, self.sizes = {}, {}, {}
        self.used = 0
        self.masks = OrderedDict()
        self.mask_bytes = 0
        self.tables = {}
        self.canonical = {}
        self.current = OrderedDict()
        self.current_bytes = 0
        self.coordinate_bytes = 0
        self.coordinate_scores = OrderedDict()
        self.scores = OrderedDict()
        self.formulas = {}
        self.score_bytes = 0
        self.parent_counts = {}
        self.parent_files = {}
        self.parent_rows = {}
        start = int(problem.start // CELL_SECONDS) * CELL_SECONDS
        stop = ceil(problem.end / CELL_SECONDS) * CELL_SECONDS
        self._layout(start, stop)
        self._compile_columns()
        self._energies()
        if len(self.blocks) > 1:
            for zone in problem.zones:
                self._compile_parents(zone)
        for lo, size in self.blocks:
            for zone in problem.zones:
                self.save((zone.group_id, lo, "labels"), self.labels(zone, lo, size))
        if self.projection is not None:
            self.blocks = ((self.blocks[0][0], max(1, self.projected_size)),)
        self._prepare_streaming()

    def _prepare_streaming(self):
        if len(self.blocks) != 1:
            return
        lo, size = self.blocks[0]
        for zone in self.problem.zones:
            if not _formula_fits(self, zone):
                self._compile_parents(zone)
                # labels() expects physical geometry before optional projection.
                physical_size = self.size if self.projection is not None else size
                self.save((zone.group_id, lo, "labels"), self.labels(zone, lo, physical_size))

    def _compile_parents(self, zone):
        """Compile disk IDs and denominators once, outside coordinate scoring."""
        db = self.store._database
        db.executescript("""
            CREATE TEMP TABLE IF NOT EXISTS timing_parents (
                zone TEXT, state TEXT, source TEXT, parent TEXT, pid INTEGER,
                seconds REAL, weighted REAL,
                PRIMARY KEY(zone,state,source,parent));
        """)
        db.execute("DELETE FROM timing_parents WHERE zone=?", (zone.group_id,))
        db.execute(
            """
            INSERT INTO timing_parents
            SELECT zone,state,source,parent,
                   row_number() OVER (ORDER BY state,source,parent)-1,
                   sum(n)*?,sum(n*confidence)*?
            FROM search_pure WHERE zone=? GROUP BY zone,state,source,parent
        """,
            (CELL_SECONDS, CELL_SECONDS, zone.group_id),
        )
        path = Path(self.store._directory.name) / f"parents-{len(self.parent_files)}"
        rows = db.execute(
            "SELECT pid,state,source,seconds,weighted FROM timing_parents WHERE zone=? ORDER BY pid",
            (zone.group_id,),
        )
        count, resident = 0, []
        with path.open("wb") as output:
            for row in rows:
                pickle.dump(row, output, protocol=5)
                count += 1
                if resident is not None:
                    resident.append(row)
                    resident = resident if count <= RESIDENT_PARENTS else None
        self.parent_files[zone.group_id] = path
        self.parent_counts[zone.group_id] = count
        self.parent_rows[zone.group_id] = resident

    def _layout(self, start, stop):
        """One projected block when it is small enough, else fixed physical blocks."""
        total = (stop - start) // CELL_SECONDS
        projection, projected = self._projection(start, total)
        if projected <= PROJECTED_CELLS:
            self.size = total
            self.blocks = ((start, total),)
            self.projection, self.projected_size = projection, projected
            return
        width = sum(len(r.channels) + 1 for r in self.problem.radars.values())
        self.size = max(SHARD_CELLS, min(total, self.budget // max(1, width)))
        episodes = self.store._database.execute(
            "SELECT count(DISTINCT parent) FROM search_pure"
        ).fetchone()[0]
        if episodes > SHARD_CELLS:
            self.size = SHARD_CELLS
        self.blocks = tuple(
            (lo, min(self.size, (stop - lo) // CELL_SECONDS))
            for lo in range(start, stop, self.size * CELL_SECONDS)
        )
        self.projection = None

    def _projection(self, start, total):
        """Label runs, each extended back by the longest timing reach, as cell runs."""
        bits = 0
        hold = max(
            (
                ceil(sum(_timing(r.timing)[i] for i in (0, 2)) / CELL_SECONDS)
                for r in self.problem.radars.values()
            ),
            default=0,
        )
        for a, n in self.store._database.execute("SELECT t,n FROM search_pure"):
            at = int((a - start) // CELL_SECONDS)
            bits |= _range(max(0, at - hold), min(total, at + n))
        return _runs(bits, 1)

    def _compile_columns(self):
        for lo, size in self.blocks:
            for rid, radar in self.problem.radars.items():
                if self.projection is None:
                    columns, coverage = _columns(self.problem, rid, lo, size, radar.channels)
                else:
                    columns, coverage = _projected_columns(
                        self.problem, rid, lo, radar.channels, self.projection, self.projected_size
                    )
                for channel, column in columns.items():
                    self.save((rid, lo, channel), column)
                self.save((rid, lo, "coverage"), coverage)

    def _energies(self):
        for lo, _size in self.blocks:
            for rid, radar in self.problem.radars.items():
                for channel in radar.channels:
                    col = self.get((rid, lo, channel))
                    self.canonical.setdefault((rid, channel), set()).update(col)
        self.maximum = {
            key: max((v for v in values if v <= 100), default=-1)
            for key, values in self.canonical.items()
        }
        self.canonical = {
            key: tuple(bisect_right(sorted(values), t) for t in range(101))
            for key, values in self.canonical.items()
        }

    def save(self, key, value):
        """Keep ``value`` resident within the budget; spill bytes raw, others pickled."""
        raw = isinstance(value, bytes)
        data = value if raw else pickle.dumps(value, protocol=5)
        if key in self.resident:
            self.used -= self.sizes.pop(key)
            del self.resident[key]
        if self.used + len(data) <= self.budget:
            self.resident[key] = value
            self.sizes[key] = len(data)
            self.used += len(data)
            return
        path = self.locations.get(key, (None, raw))[0]
        if path is None:
            path = Path(self.store._directory.name) / f"geometry-{len(self.locations)}"
        path.write_bytes(data)
        self.locations[key] = path, raw

    def get(self, key):
        if key in self.resident:
            return self.resident[key]
        path, raw = self.locations[key]
        data = path.read_bytes()
        return data if raw else pickle.loads(data)  # nosec B301

    def labels(self, zone, lo, size):
        groups = {}
        query = _LABELS_BY_PID if zone.group_id in self.parent_counts else _LABELS
        rows = self.store._database.execute(
            query,
            (zone.group_id, lo + size * CELL_SECONDS, CELL_SECONDS, lo),
        )
        for state, source, parent, confidence, start, count in rows:
            a = max(0, int((start - lo) // CELL_SECONDS))
            b = min(size, int((start - lo) // CELL_SECONDS) + count)
            key = state, source, parent, confidence
            _add_bits(groups, key, a, _range(0, b - a))
        present = _present(zone, lo, size)
        if self.projection is not None:
            groups = {
                key: _local(_project(bits << offset, self.projection))
                for key, (offset, bits) in groups.items()
            }
            present = _project(present, self.projection)
        union = 0
        for offset, bits in groups.values():
            union |= bits << offset
        ranges, position = [], 0
        while union:
            offset = (union & -union).bit_length() - 1
            local = union >> offset
            count = (local ^ (local + 1)).bit_length() - 1
            ranges.append((offset, (1 << count) - 1, position))
            position += count
            union &= ~(((1 << count) - 1) << offset)
        labels = []
        for key, (offset, bits) in groups.items():
            projected = _project(bits << offset, ranges)
            offset = (projected & -projected).bit_length() - 1
            labels.append((key, offset, projected >> offset))
        return present, tuple(labels), tuple(ranges)

    def mask(self, zone, member, pattern, signal, attenuate, exclude=None):
        model = self.problem.policy.quality.get("models", {}).get(member)
        signal = signal if model is not None else 0
        key = zone.group_id, member, signal, attenuate
        signature = tuple(pattern.items())
        if key not in self.current or self.current[key][0] != signature:
            masks = [self.channel_mask(zone, member, c, t, signal, attenuate) for c, t in signature]
            prefix = [tuple(0 for _ in self.blocks)]
            for lane in masks:
                prefix.append(tuple(a | b for a, b in zip(prefix[-1], lane, strict=True)))
            suffix = [tuple(0 for _ in self.blocks)] * (len(masks) + 1)
            for c in range(len(masks) - 1, -1, -1):
                suffix[c] = tuple(a | b for a, b in zip(suffix[c + 1], masks[c], strict=True))
            self._remember_union(key, signature, prefix, suffix)
        else:
            signature, prefix, suffix = self.current[key]
        if exclude is None:
            return prefix[-1]
        at = next(i for i, (c, _t) in enumerate(signature) if c == exclude)
        return tuple(a | b for a, b in zip(prefix[at], suffix[at + 1], strict=True))

    def _remember_union(self, key, signature, prefix, suffix):
        if key in self.current:
            _, old_prefix, old_suffix = self.current.pop(key)
            self.current_bytes -= _mask_bytes(old_prefix + old_suffix)
        self.current[key] = signature, prefix, suffix
        self.current_bytes += _mask_bytes(prefix + suffix)
        while self.current_bytes > 8 * 1024**2:
            _, (_signature, old_prefix, old_suffix) = self.current.popitem(last=False)
            self.current_bytes -= _mask_bytes(old_prefix + old_suffix)

    def channel_mask(self, zone, member, channel, threshold, signal, attenuate):
        if threshold >= self.maximum.get((member, channel), -1):
            return tuple(0 for _ in self.blocks)
        model = self.problem.policy.quality.get("models", {}).get(member)
        signal = signal if model is not None else 0
        key = (
            zone.group_id,
            member,
            channel,
            threshold if attenuate else self.canonical[member, channel][threshold],
            signal,
            attenuate,
        )
        if key not in self.masks:
            carry, result = 0, []
            radar = self.problem.radars[member]
            hold, _on, off = _timing(radar.timing)
            for lo, size in self.blocks:
                column = self.get((member, lo, channel))
                coverage = self.get((member, lo, "coverage"))
                present, _labels, ranges = self.get((zone.group_id, lo, "labels"))
                raw = self.raw(
                    {channel: column},
                    {channel: threshold},
                    present,
                    model,
                    member,
                    signal,
                    attenuate,
                )
                active, carry = _advance(
                    raw, coverage, ceil((hold + off) / CELL_SECONDS), carry, size
                )
                result.append(_project(active, ranges))
            result = tuple(result)
            size = _mask_bytes((result,))
            self.masks[key] = result, size
            self.mask_bytes += size
            while self.mask_bytes > 8 * 1024**2:
                _, (_, removed) = self.masks.popitem(last=False)
                self.mask_bytes -= removed
            return result
        self.masks.move_to_end(key)
        return self.masks[key][0]

    def raw(self, columns, thresholds, present, model, member, signal, attenuate):
        c, col = next(iter(columns.items()))
        raw = pack_mask(col, thresholds[c])
        if attenuate:
            raw = (raw & ~present) | (pack_mask(col.translate(_ATT), thresholds[c]) & present)
        if signal and model is not None:
            raw &= self.support(col, member, c, model)
        return raw

    def support(self, col, member, channel, model):
        key = member, channel
        if key not in self.tables:
            self.tables[key] = bytes(
                49 if model.supports(HISTORY_KEYS[channel], v) else 48 for v in range(256)
            )
        return int(col[::-1].translate(self.tables[key]), 2) if col else 0


def _fixed(problem, geometry, zone, pattern, rid, channel, attenuate):
    signals = (0, 1) if problem.policy.quality.get("models") else (0,)
    fixed = [[0] * len(geometry.blocks) for _ in signals]
    for member in zone.members:
        if member != rid:
            for signal in signals:
                masks = geometry.mask(zone, member, pattern[member], signal, attenuate)
                fixed[signal] = [a | b for a, b in zip(fixed[signal], masks, strict=True)]
    if rid not in zone.members:
        return signals, fixed
    for signal in signals:
        masks = geometry.mask(zone, rid, pattern[rid], signal, attenuate, exclude=channel)
        fixed[signal] = [a | b for a, b in zip(fixed[signal], masks, strict=True)]
    return signals, fixed


def _zone_costs(problem, geometry, store, zone, pattern, rid, channel, order, attenuate):

    signals, fixed = _fixed(problem, geometry, zone, pattern, rid, channel, attenuate)
    if rid not in zone.members:
        value = _mask_cost(problem, geometry, store, zone, tuple(tuple(lane) for lane in fixed))
        return [value] * len(order)
    cache_key = zone.group_id, rid, channel, tuple(tuple(lane) for lane in fixed), order, attenuate
    if cache_key in geometry.coordinate_scores:
        return geometry.coordinate_scores[cache_key]
    candidates = [
        tuple(geometry.channel_mask(zone, rid, channel, t, s, attenuate) for s in signals)
        for t in order
    ]
    results = []
    for candidate in candidates:
        masks = tuple(
            tuple(a | b for a, b in zip(fixed[signal], candidate[signal], strict=True))
            for signal in signals
        )
        key = zone.group_id, masks
        results.append(_cached_score(problem, geometry, store, zone, key, masks))
    geometry.coordinate_scores[cache_key] = results
    geometry.coordinate_bytes += _mask_bytes(cache_key[3]) + 64 * len(results)
    while geometry.coordinate_bytes > 8 * 1024**2:
        old, values = geometry.coordinate_scores.popitem(last=False)
        geometry.coordinate_bytes -= _mask_bytes(old[3]) + 64 * len(values)
    return results


def _mask_bytes(masks):
    return sum((v.bit_length() + 7) // 8 + 32 for lane in masks for v in lane) + 128


def _cached_score(problem, geometry, store, zone, key, masks):
    if key not in geometry.scores:
        value = _mask_cost(problem, geometry, store, zone, masks)
        geometry.scores[key] = value
        geometry.score_bytes += _mask_bytes(masks)
        while geometry.score_bytes > 8 * 1024**2:
            (_, old), _value = geometry.scores.popitem(last=False)
            geometry.score_bytes -= _mask_bytes(old)
        return value
    return geometry.scores[key]


def _formula(problem, geometry, store, zone):
    from .search_scores import _coefficients, linear_cost, zone_policy

    _, labels, _ranges = geometry.get((zone.group_id, geometry.blocks[0][0], "labels"))
    parents, sources = {}, {}
    statistics = {}
    for (state, source, parent, confidence), offset, bits in labels:
        signal = int(
            bool(problem.policy.quality.get("models")) and state == "present" and source != "human"
        )
        key = state + "/" + ("human" if source == "human" else "automatic")
        mass = bits.bit_count() * CELL_SECONDS
        stats = statistics.setdefault(key, [0.0] * 4)
        stats[0] += mass * confidence
        if state == "present":
            stats[1] += mass * confidence
        pk = state, source, parent
        entry = parents.setdefault(pk, [key, 0.0, 0.0, {}, signal])
        entry[1] += mass
        entry[2] += mass * confidence
        _add_bits(entry[3], 0, offset, bits)
        lane = key, confidence, signal
        sources[lane] = sources.get(lane, 0) | (bits << offset)
    for key, mass, weighted, _bits, _signal in parents.values():
        statistics[key][2] += weighted / mass
    policy = zone_policy(problem, zone)
    coefficients = {(key, i): v for key, i, v in _coefficients(statistics, policy)}
    lanes = tuple(
        (
            signal,
            bits,
            coefficients.get((key, 1), 0)
            * confidence
            * CELL_SECONDS
            * (-1 if key.startswith("present/") else 1),
        )
        for (key, confidence, signal), bits in sources.items()
    )
    episodes = []
    for (state, _source, _parent), (key, mass, weighted, local, signal) in parents.items():
        offset, bits = local[0]
        episodes.append(
            (
                signal,
                offset,
                bits,
                mass / (2 * CELL_SECONDS),
                state == "present",
                coefficients.get((key, 3), 0) * weighted / mass,
            )
        )
    return linear_cost(store, zone.group_id, statistics, policy), lanes, tuple(episodes)


def _formula_fits(geometry, zone):
    _, labels, _ranges = geometry.get((zone.group_id, geometry.blocks[0][0], "labels"))
    groups = {(key[0], key[1], key[3]) for key, _offset, _bits in labels}
    return len(groups) * geometry.blocks[0][1] // 8 <= 1024**2


def _mask_cost(problem, geometry, store, zone, masks):
    if len(geometry.blocks) == 1 and _formula_fits(geometry, zone):
        if zone.group_id not in geometry.formulas:
            geometry.formulas[zone.group_id] = _formula(problem, geometry, store, zone)
        base, lanes, episodes = geometry.formulas[zone.group_id]
        value = base + sum(
            factor * (masks[signal][0] & bits).bit_count() for signal, bits, factor in lanes
        )
        return value + sum(
            factor
            * (
                ((masks[signal][0] >> offset) & bits).bit_count() < half
                if present
                else bool((masks[signal][0] >> offset) & bits)
            )
            for signal, offset, bits, half, present, factor in episodes
        )
    return _stream_mask_cost(problem, geometry, store, zone, masks)


def _stream_mask_cost(problem, geometry, store, zone, masks):
    from .search_scores import linear_cost, zone_policy

    rows = geometry.parent_rows.get(zone.group_id)
    if rows is None:
        statistics = _spilled_statistics(geometry, store, zone, masks)
    else:
        statistics = _resident_statistics(geometry, zone, masks, rows)
    return linear_cost(store, zone.group_id, statistics, zone_policy(problem, zone))


def _resident_statistics(geometry, zone, masks, rows):
    """The disk counters' sums, in the same order, held in one resident array."""
    from .search_scores import _settle

    statistics = [{}]
    counters = array("d", bytes(len(rows) * _SEEN.size))
    _add_seen(geometry, zone, masks, counters)
    for pid, state, source, seconds, weighted in rows:
        seen, weighted_seen = counters[2 * pid], counters[2 * pid + 1]
        _settle(statistics, (state, source, pid), seconds, weighted, (seen,), (weighted_seen,))
    return statistics[0]


def _add_seen(geometry, zone, masks, counters):
    for block, (lo, _size) in enumerate(geometry.blocks):
        _, labels, _ranges = geometry.get((zone.group_id, lo, "labels"))
        for (state, source, pid, confidence), offset, bits in labels:
            signal = int(len(masks) > 1 and state == "present" and source != "human")
            seen = ((masks[signal][block] >> offset) & bits).bit_count() * CELL_SECONDS
            counters[2 * pid] += seen
            counters[2 * pid + 1] += seen * confidence


def _spilled_statistics(geometry, store, zone, masks):
    from .search_scores import _settle

    statistics = [{}]
    with tempfile.TemporaryFile(dir=store._directory.name) as counters:
        counters.truncate(geometry.parent_counts[zone.group_id] * _SEEN.size)
        _count_seen(geometry, zone, masks, counters)
        with geometry.parent_files[zone.group_id].open("rb") as parents:
            for _ in range(geometry.parent_counts[zone.group_id]):
                pid, state, source, seconds, weighted = pickle.load(parents)  # nosec B301
                counters.seek(pid * _SEEN.size)
                seen, weighted_seen = _SEEN.unpack(counters.read(_SEEN.size))
                _settle(
                    statistics, (state, source, pid), seconds, weighted, (seen,), (weighted_seen,)
                )
    return statistics[0]


def _count_seen(geometry, zone, masks, counters):
    """Update fixed-width disk counters; resident work stays within one label block."""
    for block, (lo, _size) in enumerate(geometry.blocks):
        _, labels, _ranges = geometry.get((zone.group_id, lo, "labels"))
        for (state, source, pid, confidence), offset, bits in labels:
            signal = int(len(masks) > 1 and state == "present" and source != "human")
            seen = ((masks[signal][block] >> offset) & bits).bit_count() * CELL_SECONDS
            counters.seek(pid * _SEEN.size)
            previous, weighted = _SEEN.unpack(counters.read(_SEEN.size))
            counters.seek(pid * _SEEN.size)
            counters.write(_SEEN.pack(previous + seen, weighted + seen * confidence))


def timed_coordinate_costs(problem, store, pattern, rid, channel, candidates, *, attenuate=False):
    """Reuse compiled geometry and radar masks throughout every search sweep."""
    if not hasattr(store, "_timing_geometry"):
        store._timing_geometry = Geometry(problem, store)
    order = tuple(sorted(candidates))
    costs = [0.0] * len(order)
    for zone in sorted(problem.zones, key=lambda z: z.group_id):
        values = _zone_costs(
            problem, store._timing_geometry, store, zone, pattern, rid, channel, order, attenuate
        )
        costs = [a + b for a, b in zip(costs, values, strict=True)]
    return {t: round(v, 9) for t, v in zip(order, costs, strict=True)}

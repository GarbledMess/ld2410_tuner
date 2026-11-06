"""Disk-backed, lossless pure-cell joins and per-episode vector multiplicities."""

import re
from collections import OrderedDict
from heapq import heapify, heappop, heappush
from itertools import chain, compress, groupby
from math import nextafter

from ...history import pace
from .constants import CELL_SECONDS, SHARD_CELLS
from .digest import cell_ceil, cell_floor
from .evidence import _labels

_CHANGED = re.compile(rb"[^\x00]+")
_COVERED = bytes(value == 1 for value in range(256))
RAW_ENERGY_BYTES = 8 * 1024**2
_LABEL_BATCH = 256


def _schema(store):
    _close_geometry(store)
    for name in (
        "_timing_geometry",
        "_packed_key",
        "_packed_block",
        "_cost_coefficients",
        "_decision_vectors",
        "_column_blocks",
        "_histogram_zones",
        "_histogram_bytes",
        "_raw_energies",
        "_raw_energy_bytes",
    ):
        if hasattr(store, name):
            delattr(store, name)
    db = store._database
    db.executescript("""
        DROP TABLE IF EXISTS search_raw;
        DROP VIEW IF EXISTS search_vectors;
        DROP TABLE IF EXISTS search_joined;
        DROP TABLE IF EXISTS search_single;
        DROP TABLE IF EXISTS search_pure;
        DROP TABLE IF EXISTS search_groups;
        DROP TABLE IF EXISTS search_bounds;
        CREATE TABLE search_raw (radar TEXT, t REAL, stop REAL, vector BLOB,
                                 PRIMARY KEY(radar,t)) WITHOUT ROWID;
        CREATE TABLE search_joined (zone TEXT,t REAL,stop REAL,vector BLOB);
        CREATE TABLE search_single (zone TEXT,radar TEXT,lo REAL,hi REAL,first REAL);
        CREATE VIEW search_vectors AS
            SELECT zone,t,stop,vector FROM search_joined
            UNION ALL
            SELECT s.zone,max(r.t,s.lo),min(r.stop,s.hi),r.vector
            FROM search_single s JOIN search_raw r
              ON r.radar=s.radar AND r.t>=s.first AND r.t<s.hi AND r.stop>s.lo;
        CREATE TABLE search_pure (zone TEXT,state TEXT,source TEXT,parent TEXT,
                                 confidence REAL,vector BLOB,t REAL,n INTEGER);
        CREATE TABLE search_groups (zone TEXT,state TEXT,source TEXT,parent TEXT,
                                   confidence REAL,vector BLOB,n INTEGER,
                                   PRIMARY KEY(zone,state,source,parent,confidence,vector))
                                   WITHOUT ROWID;
        CREATE TABLE search_bounds (zone TEXT PRIMARY KEY, max_n INTEGER,
                                    present INTEGER, empty INTEGER);
    """)
    return db


def _close_geometry(store):
    geometry = getattr(store, "_timing_geometry", None)
    if geometry is not None:
        geometry.close()


def _fold_radar(problem, db, rid, shard_cells, *, observe=False):
    radar = problem.radars[rid]
    observed = {channel: set() for channel in radar.channels} if observe else None
    previous = None
    for day in problem.days.dates(rid):
        pace.rest()
        digest = problem.days.digest(rid, day, radar.channels)
        if digest is None:
            continue
        try:
            _save_day(db, rid, digest, shard_cells)
            if observed is not None:
                observed, previous = _observe_day(problem, digest, observed, previous)
        finally:
            close = getattr(digest.partial, "close", None)
            if close is not None:
                close()
    return observed


def _observe_day(problem, digest, observed, previous):
    # Overlapping/out-of-order digests may overwrite raw rows; retain the SQL
    # fallback rather than keeping energies from an overwritten vector.
    if observed is None or (previous is not None and digest.k0 < previous):
        return None, previous
    lo = max(0, cell_floor(problem.start, CELL_SECONDS) - digest.k0)
    hi = min(len(digest.cov), cell_ceil(problem.end, CELL_SECONDS) - digest.k0)
    if hi > lo and problem.end > problem.start:
        covered = bytes(digest.cov[lo:hi]).translate(_COVERED)
        full = covered.count(1) == len(covered)
        for channel, values in observed.items():
            column = digest.channels[channel][lo:hi]
            if not full:
                column = bytes(compress(column, covered))
            values.update(value for value in set(column) if value <= 100)
    return observed, digest.k0 + len(digest.cov)


def _retain_raw_energies(store, rid, observed):
    if observed is None:
        return
    values = {channel: bytes(sorted(energies)) for channel, energies in observed.items()}
    size = 512 + len(rid.encode()) + sum(256 + len(column) for column in values.values())
    store._raw_energies[rid] = values, size
    store._raw_energy_bytes += size
    while store._raw_energy_bytes > RAW_ENERGY_BYTES:
        _rid, (_values, removed) = store._raw_energies.popitem(last=False)
        store._raw_energy_bytes -= removed


def _raw_energy_members(problem):
    if any(
        any(radar.timing.get(key, 0) for key in ("timeout", "off_delay"))
        for radar in problem.radars.values()
    ):
        return set()
    return {zone.members[0] for zone in problem.zones if len(zone.members) == 1}


def _save_day(db, rid, digest, shard_cells):
    """Insert each run of identical covered (coverage, channels) cells as one row."""
    width = len(digest.channels)
    rows = bytearray(len(digest.cov) * width)
    for c, column in enumerate(digest.channels):
        rows[c::width] = column
    rows = bytes(rows)
    pending = []
    for start, stop in vector_runs((digest.cov, *digest.channels), len(digest.cov)):
        if digest.cov[start] == 1:
            lo = (digest.k0 + start) * digest.resolution
            hi = lo + (stop - start) * digest.resolution
            pending.append((rid, lo, hi, rows[start * width : (start + 1) * width]))
        if len(pending) >= shard_cells:
            _flush_raw(db, pending)
    _flush_raw(db, pending)


def vector_runs(columns, n):
    """``(start, stop)`` runs of identical cross-column vectors, found in C."""
    change = 0
    seen = []
    for column in columns:
        if column in seen:
            continue
        seen.append(column)
        if len(column) != n:
            raise ValueError("Digest columns differ in length")
        value = int.from_bytes(column, "little")
        change |= value ^ (value >> 8)
    flags = change.to_bytes(max(n, (change.bit_length() + 7) // 8), "little")[: max(0, n - 1)]
    cuts = list(
        chain.from_iterable(range(m.start() + 1, m.end() + 1) for m in _CHANGED.finditer(flags))
    )
    return list(zip([0, *cuts], [*cuts, n], strict=True)) if n else []


def _flush_raw(db, pending):
    pace.rest()
    batches = len(pending) // 64 * 64
    rows = iter(pending[:batches])
    db.executemany(
        "INSERT OR REPLACE INTO search_raw VALUES "
        "(?,?,?,?),(?,?,?,?),(?,?,?,?),(?,?,?,?),"
        "(?,?,?,?),(?,?,?,?),(?,?,?,?),(?,?,?,?),"
        "(?,?,?,?),(?,?,?,?),(?,?,?,?),(?,?,?,?),"
        "(?,?,?,?),(?,?,?,?),(?,?,?,?),(?,?,?,?),"
        "(?,?,?,?),(?,?,?,?),(?,?,?,?),(?,?,?,?),"
        "(?,?,?,?),(?,?,?,?),(?,?,?,?),(?,?,?,?),"
        "(?,?,?,?),(?,?,?,?),(?,?,?,?),(?,?,?,?),"
        "(?,?,?,?),(?,?,?,?),(?,?,?,?),(?,?,?,?),"
        "(?,?,?,?),(?,?,?,?),(?,?,?,?),(?,?,?,?),"
        "(?,?,?,?),(?,?,?,?),(?,?,?,?),(?,?,?,?),"
        "(?,?,?,?),(?,?,?,?),(?,?,?,?),(?,?,?,?),"
        "(?,?,?,?),(?,?,?,?),(?,?,?,?),(?,?,?,?),"
        "(?,?,?,?),(?,?,?,?),(?,?,?,?),(?,?,?,?),"
        "(?,?,?,?),(?,?,?,?),(?,?,?,?),(?,?,?,?),"
        "(?,?,?,?),(?,?,?,?),(?,?,?,?),(?,?,?,?),"
        "(?,?,?,?),(?,?,?,?),(?,?,?,?),(?,?,?,?)",
        (tuple(chain.from_iterable(group)) for group in zip(*[rows] * 64, strict=True)),
    )
    db.executemany("INSERT OR REPLACE INTO search_raw VALUES (?,?,?,?)", pending[batches:])
    pending.clear()


def _joined(db, members):
    if len(members) == 1:
        yield from db.execute(
            "SELECT t,stop,vector FROM search_raw WHERE radar=? ORDER BY t", members
        )
        return
    streams = [
        iter(db.execute("SELECT t,stop,vector FROM search_raw WHERE radar=? ORDER BY t", (member,)))
        for member in members
    ]
    yield from _join_streams(streams)


def _join_streams(streams):
    current = [next(stream, None) for stream in streams]
    if None in current:
        return
    vectors = [row[2] for row in current]
    latest = max(row[0] for row in current)
    ends = [(row[1], i) for i, row in enumerate(current)]
    heapify(ends)
    while ends:
        stop = ends[0][0]
        if latest < stop:
            yield latest, stop, b"".join(vectors)
        latest = _advance_join(streams, vectors, ends, stop, latest)
        if latest is None:
            return


def _advance_join(streams, vectors, ends, stop, latest):
    # Install ties together, matching the chronological boundary scan even when
    # an overlapping digest contributes another run ending at the same instant.
    updates = []
    while ends and ends[0][0] == stop:
        _end, i = heappop(ends)
        row = next(streams[i], None)
        if row is None:
            return None
        latest = max(latest, row[0])
        vectors[i] = row[2]
        updates.append((row[1], i))
    for update in updates:
        heappush(ends, update)
    return latest


def _compile_zone(problem, db, zone, shard_cells):
    members = tuple(sorted(zone.members))
    if len(members) == 1:
        _compile_single(problem, db, zone, members[0], shard_cells)
        return
    labels = iter(_labels(zone.truth))
    label = next(labels, None)
    vectors, pure = [], []
    for lo, hi, vector in _joined(db, members):
        lo, hi = max(lo, problem.start), min(hi, problem.end)
        if hi <= lo:
            continue
        while label is not None and label[1] <= lo:
            label = next(labels, None)
        vectors.append((zone.group_id, lo, hi, vector))
        label = _pure_runs(labels, label, zone.group_id, lo, hi, vector, pure)
        if len(vectors) >= shard_cells:
            _flush(db, vectors, pure)
    _flush(db, vectors, pure)


def _windows(lo, hi, shard_cells):
    while lo < hi:
        end = min(hi, lo + shard_cells * CELL_SECONDS)
        yield lo, end
        lo = end


def _raw_lookback(db, rid):
    (longest,) = db.execute("SELECT max(stop-t) FROM search_raw WHERE radar=?", (rid,)).fetchone()
    return longest or 0


def _single_start(db, rid, lo, lookback=None):
    lookback = _raw_lookback(db, rid) if lookback is None else lookback
    row = db.execute(
        "SELECT t FROM search_raw WHERE radar=? AND t>=? AND t<=? AND stop>? ORDER BY t LIMIT 1",
        (rid, nextafter(lo - lookback, float("-inf")), lo, lo),
    ).fetchone()
    return row[0] if row else lo


def _compile_single(problem, db, zone, rid, shard_cells):
    """Overlay one stream in SQLite, retaining every overlapping raw run."""
    lookback = _raw_lookback(db, rid)
    for lo, hi in _windows(problem.start, problem.end, shard_cells):
        pace.rest()
        db.execute(
            "INSERT INTO search_single VALUES (?,?,?,?,?)",
            (zone.group_id, rid, lo, hi, _single_start(db, rid, lo, lookback)),
        )
    db.execute("""CREATE TEMP TABLE IF NOT EXISTS search_windows
        (state TEXT,source TEXT,parent TEXT,confidence REAL,lo REAL,hi REAL,earliest REAL)""")
    pending = []
    for a, b, span in _labels(zone.truth):
        lo = cell_ceil(max(a, problem.start), CELL_SECONDS) * CELL_SECONDS
        hi = cell_floor(min(b, problem.end), CELL_SECONDS) * CELL_SECONDS
        for start, stop in _windows(lo, hi, shard_cells):
            pending.append(_label_window(span, start, stop, lookback))
        if len(pending) >= _LABEL_BATCH:
            _single_labels(db, zone.group_id, rid, pending)
    _single_labels(db, zone.group_id, rid, pending)


def _label_window(span, lo, hi, lookback):
    earliest = nextafter(lo - lookback, float("-inf"))
    return span.state, span.source, span.episode_id, span.confidence, lo, hi, earliest


def _single_labels(db, group, rid, pending):
    """One statement per batch of label windows, keeping their order and each one's rows in time."""
    pace.rest()
    db.execute("DELETE FROM search_windows")
    db.executemany("INSERT INTO search_windows VALUES (?,?,?,?,?,?,?)", pending)
    db.execute(
        """INSERT INTO search_pure
        SELECT ?1,w.state,w.source,w.parent,w.confidence,r.vector,max(r.t,w.lo),
               CAST((min(r.stop,w.hi)-max(r.t,w.lo))/?2 AS INTEGER)
        FROM search_windows w CROSS JOIN search_raw r
        WHERE r.radar=?3 AND r.t<w.hi AND r.stop>w.lo AND r.t>=coalesce(
            (SELECT s.t FROM search_raw s
             WHERE s.radar=?3 AND s.t>=w.earliest AND s.t<=w.lo AND s.stop>w.lo
             ORDER BY s.t LIMIT 1), w.lo)
        ORDER BY w.rowid,r.t""",
        (group, CELL_SECONDS, rid),
    )
    pending.clear()


def _pure_runs(labels, label, group_id, lo, hi, vector, pure):
    while label is not None and label[0] < hi:
        a = cell_ceil(max(lo, label[0]), CELL_SECONDS) * CELL_SECONDS
        b = cell_floor(min(hi, label[1]), CELL_SECONDS) * CELL_SECONDS
        if b > a:
            span = label[2]
            pure.append(
                (
                    group_id,
                    span.state,
                    span.source,
                    span.episode_id,
                    span.confidence,
                    vector,
                    a,
                    int((b - a) / CELL_SECONDS),
                )
            )
        if label[1] >= hi:
            return label
        label = next(labels, None)
    return label


def _flush(db, vectors, pure):
    pace.rest()
    db.executemany("INSERT INTO search_joined VALUES (?,?,?,?)", vectors)
    db.executemany("INSERT INTO search_pure VALUES (?,?,?,?,?,?,?,?)", pure)
    vectors.clear()
    pure.clear()


def collect_training(problem, store, *, shard_cells=SHARD_CELLS):
    """Read one radar-day at a time and preserve full pure-cell multiplicities.

    Mixed and partial cells remain available to the common evaluator, never
    assigned midpoint truth by search. SQLite's bounded page cache and disk
    grouping avoid a whole-history vector, mask, or parent array in memory.
    """
    if not 0 < shard_cells <= SHARD_CELLS:
        raise ValueError("Invalid search shard size")
    db = _schema(store)
    store._raw_energies, store._raw_energy_bytes = OrderedDict(), 0
    singles = _raw_energy_members(problem)
    for rid in sorted(problem.radars):
        observed = _fold_radar(problem, db, rid, shard_cells, observe=rid in singles)
        _retain_raw_energies(store, rid, observed)
    for zone in sorted(problem.zones, key=lambda z: z.group_id):
        if zone.members:
            _compile_zone(problem, db, zone, shard_cells)
    db.execute("""INSERT INTO search_groups
        SELECT zone,state,source,parent,confidence,vector,sum(n) FROM search_pure
        GROUP BY zone,state,source,parent,confidence,vector""")
    db.execute("CREATE INDEX search_pure_time ON search_pure(zone,t)")
    db.execute(
        "INSERT INTO search_bounds SELECT zone,max(n),"
        "sum(CASE WHEN state='present' THEN n END),"
        "sum(CASE WHEN state='not_present' THEN n END) FROM search_pure GROUP BY zone"
    )
    db.commit()


def episode_groups(store, zone, *, folded=True):
    if folded:
        sql = """SELECT state,source,parent,confidence,vector,n FROM search_groups
                 WHERE zone=? ORDER BY state,source,parent,confidence,vector"""
    else:
        sql = """SELECT state,source,parent,confidence,vector,n FROM search_pure
                 WHERE zone=? ORDER BY state,source,parent,confidence,vector"""
    rows = store._database.execute(sql, (zone,))
    if not folded:
        rows = (row[:5] + (1,) for row in rows for _ in range(row[5]))
    return groupby(rows, key=lambda row: row[:3])


def histograms(problem, store, rid, channel):
    result = {state: {} for state in ("present", "not_present")}
    for zone in problem.zones:
        if rid not in zone.members:
            continue
        at = tuple(sorted(zone.members)).index(rid) * 18 + channel
        rows = store._database.execute(
            "SELECT state,source,vector,n FROM search_groups WHERE zone=?", (zone.group_id,)
        )
        _histogram_rows(result, rows, at)
    return tuple(_primary_bins(result[state]) for state in ("present", "not_present"))


def _histogram_rows(result, rows, at):
    for state, source, vector, count in rows:
        value = vector[at]
        if value <= 100:
            bins = result[state].setdefault(source, [0.0] * 101)
            bins[value] += count * CELL_SECONDS


def _primary_bins(sources):
    if "human" in sources:
        return sources["human"]
    return [sum(bins[v] for bins in sources.values()) for v in range(101)]


def observed_energies(problem, store, rid, channel):
    energies = set()
    for zone in problem.zones:
        if rid in zone.members:
            at = tuple(sorted(zone.members)).index(rid) * 18 + channel
            rows = store._database.execute(
                "SELECT vector FROM search_vectors WHERE zone=?", (zone.group_id,)
            )
            energies.update(row[0][at] for row in rows if row[0][at] <= 100)
    return tuple(sorted(energies))


def pure_cells(store):
    """Pure labelled training cells per state, summed over zones and sources."""
    counts = store._database.execute("SELECT sum(present),sum(empty) FROM search_bounds").fetchone()
    return {
        state: n
        for state, n in zip(("present", "not_present"), counts, strict=True)
        if n is not None
    }

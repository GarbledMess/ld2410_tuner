"""Disk-backed, lossless pure-cell joins and per-episode vector multiplicities."""

from itertools import groupby

from ...history import pace
from .constants import CELL_SECONDS, SHARD_CELLS
from .digest import cell_ceil, cell_floor
from .evidence import _labels


def _schema(store):
    for name in (
        "_timing_geometry",
        "_packed_key",
        "_packed_block",
        "_cost_coefficients",
        "_decision_vectors",
        "_column_blocks",
    ):
        if hasattr(store, name):
            delattr(store, name)
    db = store._database
    db.executescript("""
        DROP TABLE IF EXISTS search_raw;
        DROP TABLE IF EXISTS search_vectors;
        DROP TABLE IF EXISTS search_pure;
        DROP TABLE IF EXISTS search_groups;
        CREATE TABLE search_raw (radar TEXT, t REAL, stop REAL, vector BLOB,
                                 PRIMARY KEY(radar,t));
        CREATE TABLE search_vectors (zone TEXT,t REAL,stop REAL,vector BLOB);
        CREATE TABLE search_pure (zone TEXT,state TEXT,source TEXT,parent TEXT,
                                 confidence REAL,vector BLOB,t REAL,n INTEGER);
        CREATE TABLE search_groups (zone TEXT,state TEXT,source TEXT,parent TEXT,
                                   confidence REAL,vector BLOB,n INTEGER);
    """)
    return db


def _fold_radar(problem, db, rid, shard_cells):
    radar = problem.radars[rid]
    for day in problem.days.dates(rid):
        pace.rest()
        digest = problem.days.digest(rid, day, radar.channels)
        if digest is None:
            continue
        try:
            _save_day(db, rid, digest, shard_cells)
        finally:
            close = getattr(digest.partial, "close", None)
            if close is not None:
                close()


def _save_day(db, rid, digest, shard_cells):
    pending = []
    offset = 0
    for vector, group in groupby(zip(digest.cov, *digest.channels, strict=True)):
        count = sum(1 for _ in group)
        if vector[0] == 1:
            lo = (digest.k0 + offset) * digest.resolution
            hi = lo + count * digest.resolution
            pending.append((rid, lo, hi, bytes(vector[1:])))
        offset += count
        if len(pending) >= shard_cells:
            _flush_raw(db, pending)
    _flush_raw(db, pending)


def _flush_raw(db, pending):
    pace.rest()
    db.executemany("INSERT OR REPLACE INTO search_raw VALUES (?,?,?,?)", pending)
    pending.clear()


def _joined(db, members):
    streams = [
        iter(db.execute("SELECT t,stop,vector FROM search_raw WHERE radar=? ORDER BY t", (member,)))
        for member in members
    ]
    current = [next(stream, None) for stream in streams]
    while all(row is not None for row in current):
        lo = max(row[0] for row in current)
        hi = min(row[1] for row in current)
        if lo < hi:
            yield lo, hi, b"".join(row[2] for row in current)
        for i, row in enumerate(current):
            if row[1] == hi:
                current[i] = next(streams[i], None)


def _compile_zone(problem, db, zone, shard_cells):
    members = tuple(sorted(zone.members))
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
    db.executemany("INSERT INTO search_vectors VALUES (?,?,?,?)", vectors)
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
    for rid in sorted(problem.radars):
        _fold_radar(problem, db, rid, shard_cells)
    for zone in sorted(problem.zones, key=lambda z: z.group_id):
        if zone.members:
            _compile_zone(problem, db, zone, shard_cells)
    db.execute("""INSERT INTO search_groups
        SELECT zone,state,source,parent,confidence,vector,sum(n) FROM search_pure
        GROUP BY zone,state,source,parent,confidence,vector""")
    db.execute("CREATE INDEX search_episode ON search_groups(zone,state,source,parent)")
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
    rows = store._database.execute("SELECT state, sum(n) FROM search_groups GROUP BY state")
    return dict(rows)

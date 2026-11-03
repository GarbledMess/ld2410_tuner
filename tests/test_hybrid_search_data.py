"""Exact columnar raw-run insertion over synthetic digests."""

import random
import sqlite3
from itertools import groupby

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.engine.hybrid import search_data
from tuner_under_test.engine.hybrid.digest import DayDigest


@pytest.mark.parametrize("n", (0, 1, 200))
@pytest.mark.parametrize("shard", (1, 7, 4096))
def test_raw_run_batches_match_scalar(n, shard):
    rng = random.Random(810 + n)
    columns = tuple(bytes(rng.choice((0, 40, 80, 255)) for _ in range(n)) for _ in range(18))
    coverage = bytes(rng.choice((0, 1, 2)) for _ in range(n))
    digest = DayDigest(6, 298300000, n, columns, coverage, {})
    expected, offset = [], 0
    for vector, group in groupby(zip(coverage, *columns, strict=True)):
        count = sum(1 for _ in group)
        if vector[0] == 1:
            lo = (digest.k0 + offset) * 6
            expected.append(("r", lo, lo + count * 6, bytes(vector[1:])))
        offset += count

    class Database:
        def __init__(self, db):
            self.db = db
            self.sizes = []

        def executemany(self, sql, rows):
            rows = list(rows)
            self.sizes.append(len(rows))
            self.db.executemany(sql, rows)

    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE search_raw(radar TEXT,t REAL,stop REAL,vector BLOB)")
        batches = Database(db)
        search_data._save_day(batches, "r", digest, shard)
        assert db.execute("SELECT * FROM search_raw ORDER BY rowid").fetchall() == expected
        assert max(batches.sizes, default=0) <= shard


def test_raw_day_interleaving_is_frozen_once(monkeypatch):
    from builtins import bytes as immutable

    n = 100
    digest = DayDigest(6, 0, n, tuple(immutable(range(n)) for _ in range(18)), b"\1" * n, {})
    copies = []

    def counted(value):
        copies.append(len(value))
        return immutable(value)

    monkeypatch.setattr(search_data, "bytes", counted, raising=False)
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE search_raw(radar TEXT,t REAL,stop REAL,vector BLOB)")
        search_data._save_day(db, "r", digest, 7)
        assert db.execute("SELECT count(*) FROM search_raw").fetchone() == (n,)
    assert copies == [n * 18]


def test_multi_member_join_reads_each_run_boundary_once():
    class Row(tuple):
        starts = 0

        def __getitem__(self, key):
            if key == 0:
                type(self).starts += 1
            return super().__getitem__(key)

    rows = {
        str(member): [Row((at, at + 7, bytes([member]))) for at in range(member, 80, 7)]
        for member in range(12)
    }

    class Database:
        def execute(self, _sql, parameters):
            return iter(rows[parameters[0]])

    got = list(search_data._joined(Database(), tuple(rows)))
    assert got
    assert Row.starts <= sum(map(len, rows.values()))


@pytest.mark.parametrize("seed", range(10))
def test_multi_member_join_matches_scalar_boundary_scan(seed):
    rng = random.Random(981 + seed)
    members = tuple(str(member) for member in range(12))
    rows = {}
    for member in members:
        at, stream = rng.randrange(8), []
        for _ in range(30):
            end = at + rng.randrange(1, 16)
            stream.append((at, end, bytes([rng.randrange(101)])))
            at = end + rng.randrange(5)
        rows[member] = stream

    streams = [iter(rows[member]) for member in members]
    current = [next(stream, None) for stream in streams]
    expected = []
    while all(row is not None for row in current):
        lo, hi = max(row[0] for row in current), min(row[1] for row in current)
        if lo < hi:
            expected.append((lo, hi, b"".join(row[2] for row in current)))
        for i, row in enumerate(current):
            if row[1] == hi:
                current[i] = next(streams[i], None)

    class Database:
        def execute(self, _sql, parameters):
            return iter(rows[parameters[0]])

    assert list(search_data._joined(Database(), members)) == expected


def test_raw_day_bulk_insert_reduces_sql_statement_work():
    n = 256
    digest = DayDigest(6, 0, n, (bytes(range(n)),) * 18, b"\1" * n, {})
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE search_raw(radar TEXT,t REAL,stop REAL,vector BLOB)")
        statements = []
        db.set_trace_callback(statements.append)
        search_data._save_day(db, "r", digest, 4096)
        db.set_trace_callback(None)
        inserted = [sql for sql in statements if sql.startswith("INSERT")]
        assert len(inserted) <= n // 8
        assert db.execute("SELECT count(*) FROM search_raw").fetchone() == (n,)


def test_pure_cell_counts_reuse_finalized_zone_summaries(tmp_path):
    from test_hybrid_search import problem
    from tuner_under_test.engine.hybrid.training import TrainingStore

    p = problem({"a": [(70,), (20,), (0,)]})
    with TrainingStore(tmp_path) as store:
        search_data.collect_training(p, store)
        expected = dict(
            store._database.execute("SELECT state,sum(n) FROM search_groups GROUP BY state")
        )
        statements = []
        store._database.set_trace_callback(statements.append)
        assert search_data.pure_cells(store) == expected
        assert search_data.pure_cells(store) == expected
        store._database.set_trace_callback(None)
        assert not any(
            "FROM search_groups" in sql or "FROM search_pure" in sql for sql in statements
        )


def test_grouped_episode_stream_does_not_sort_its_existing_key(tmp_path):
    from test_hybrid_search import problem
    from tuner_under_test.engine.hybrid.training import TrainingStore

    p = problem({"a": [(70,), (20,), (70,), (0,)]})
    with TrainingStore(tmp_path) as store:
        search_data.collect_training(p, store)
        plan = store._database.execute(
            "EXPLAIN QUERY PLAN SELECT state,source,parent,confidence,vector,n "
            "FROM search_groups WHERE zone=? ORDER BY state,source,parent,confidence,vector",
            (p.zones[0].group_id,),
        ).fetchall()
        assert not any("TEMP B-TREE" in row[-1] for row in plan)
        folded = [(key, list(rows)) for key, rows in search_data.episode_groups(store, "z")]
        unfolded = [
            (key, list(rows)) for key, rows in search_data.episode_groups(store, "z", folded=False)
        ]
        for (key, groups), (other, cells) in zip(folded, unfolded, strict=True):
            assert key == other
            assert sum(row[-1] for row in groups) == len(cells)


def test_single_member_vector_storage_only_writes_window_descriptors(tmp_path):
    from test_hybrid_search import problem
    from tuner_under_test.engine.hybrid.training import TrainingStore

    p = problem({"a": [(i % 101,) for i in range(2400)]})
    shard = 4096
    with TrainingStore(tmp_path) as store:
        db = search_data._schema(store)
        search_data._fold_radar(p, db, "a", shard)
        expected = db.execute(
            "SELECT ?,t,stop,vector FROM search_raw WHERE radar=? ORDER BY t", ("z", "a")
        ).fetchall()
        before = db.total_changes
        search_data._compile_zone(p, db, p.zones[0], shard)
        changed = db.total_changes - before
        (pure,) = db.execute("SELECT count(*) FROM search_pure").fetchone()
        assert changed <= pure + 1
        actual = db.execute("SELECT zone,t,stop,vector FROM search_vectors ORDER BY t").fetchall()
        assert actual == expected


@pytest.mark.parametrize("shard", (1, 7, 4096))
@pytest.mark.parametrize("bounds", ((0, 120), (1, 119), (25, 75)))
def test_single_vector_view_matches_eager_windows_with_overlaps(tmp_path, shard, bounds):
    from dataclasses import replace

    from test_hybrid_search import problem
    from tuner_under_test.engine.hybrid.training import TrainingStore

    p = replace(problem({"a": [(i,) for i in range(20)]}), start=bounds[0], end=bounds[1])
    with TrainingStore(tmp_path) as store:
        db = search_data._schema(store)
        search_data._fold_radar(p, db, "a", shard)
        db.executemany(
            "INSERT INTO search_raw VALUES (?,?,?,?)",
            [("a", 2, 50, bytes([80]) * 18), ("a", 55, 140, bytes([90]) * 18)],
        )
        expected = []
        for lo, hi in search_data._windows(p.start, p.end, shard):
            first = search_data._single_start(db, "a", lo)
            expected.extend(
                db.execute(
                    "SELECT ?,max(t,?),min(stop,?),vector FROM search_raw "
                    "WHERE radar=? AND t>=? AND t<? AND stop>? ORDER BY t",
                    ("z", lo, hi, "a", first, hi, lo),
                )
            )
        search_data._compile_zone(p, db, p.zones[0], shard)
        actual = db.execute("SELECT zone,t,stop,vector FROM search_vectors").fetchall()
        assert sorted(actual) == sorted(expected)


def test_raw_vectors_use_one_disk_tree_for_storage_and_time_lookup(tmp_path):
    from tuner_under_test.engine.hybrid.training import TrainingStore

    with TrainingStore(tmp_path) as store:
        db = search_data._schema(store)
        trees = db.execute(
            "SELECT count(*) FROM sqlite_schema WHERE tbl_name='search_raw' AND rootpage>0"
        ).fetchone()[0]
        assert trees == 1
        db.executemany(
            "INSERT OR REPLACE INTO search_raw VALUES (?,?,?,?)",
            [("r", 0, 12, b"a"), ("r", 6, 12, b"b"), ("r", 0, 6, b"c")],
        )
        assert db.execute("SELECT * FROM search_raw ORDER BY radar,t").fetchall() == [
            ("r", 0, 6, b"c"),
            ("r", 6, 12, b"b"),
        ]


def test_vector_run_boundaries_only_decode_distinct_columns(monkeypatch):
    from builtins import int as integer

    column = bytes([1, 2, 2, 3])
    calls = []

    class Integers:
        @staticmethod
        def from_bytes(value, order):
            calls.append(value)
            return integer.from_bytes(value, order)

    monkeypatch.setattr(search_data, "int", Integers, raising=False)
    assert search_data.vector_runs((column,) * 18, 4) == [(0, 1), (1, 3), (3, 4)]
    assert calls == [column]


def test_dense_run_boundaries_use_one_match_per_contiguous_change_region(monkeypatch):
    original = search_data._CHANGED
    matches = []

    class Changes:
        def finditer(self, values):
            for match in original.finditer(values):
                matches.append(match.span())
                yield match

    monkeypatch.setattr(search_data, "_CHANGED", Changes())
    n = 10000
    columns = (bytes(i % 2 for i in range(n)),)
    assert search_data.vector_runs(columns, n) == [(i, i + 1) for i in range(n)]
    assert len(matches) == 1


def test_dense_raw_insert_amortizes_statement_setup_over_sixty_four_rows():
    n = 1024
    digest = DayDigest(6, 0, n, (bytes(i % 101 for i in range(n)),) * 18, b"\1" * n, {})
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE search_raw(radar TEXT,t REAL,stop REAL,vector BLOB)")
        statements = []
        db.set_trace_callback(statements.append)
        search_data._save_day(db, "r", digest, 4096)
        db.set_trace_callback(None)
        assert len([sql for sql in statements if sql.startswith("INSERT")]) <= n // 64
        assert db.execute("SELECT count(*) FROM search_raw").fetchone() == (n,)


@pytest.mark.parametrize("shard", (1, 7, 4096))
def test_single_view_and_pure_rows_preserve_earlier_overlapping_runs(tmp_path, shard):
    from dataclasses import replace

    from test_hybrid_evidence import truth
    from test_hybrid_search import problem
    from tuner_under_test.engine.hybrid.training import TrainingStore

    p = replace(
        problem({"r": [(80,), (20,)]}, spans=[truth(18, 24, episode="p")]),
        start=18,
        end=24,
    )
    with TrainingStore(tmp_path) as store:
        db = search_data._schema(store)
        db.executemany(
            "INSERT INTO search_raw VALUES (?,?,?,?)",
            [("r", 0, 60, bytes([80]) * 18), ("r", 6, 12, bytes([20]) * 18)],
        )
        expected = []
        for lo, hi in search_data._windows(p.start, p.end, shard):
            expected.extend(
                (p.zones[0].group_id, max(a, lo), min(b, hi), vector)
                for a, b, vector in search_data._joined(db, ("r",))
                if a < hi and b > lo
            )
        search_data._compile_zone(p, db, p.zones[0], shard)
        actual = db.execute("SELECT zone,t,stop,vector FROM search_vectors").fetchall()
        assert actual == expected
        pure = db.execute("SELECT vector,t,n FROM search_pure").fetchall()
        assert pure == [(bytes([80]) * 18, 18, 1)]

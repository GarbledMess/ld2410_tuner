"""Bounded append-only training shards and disk-indexed episode statistics."""

import sqlite3
import tempfile
from pathlib import Path

from .constants import CHANNELS, SHARD_CELLS, SPILL_BYTES


class TrainingStore:
    """Spool channels by byte budget; never reload a complete history vector.

    The small SQLite index also settles original episode keys across gaps and
    confidence fragments. Its page cache is bounded independently of history.
    Only a shard's 18 channel vectors and its opaque lineage metadata are read
    at once. No file descriptor is retained per radar, zone, source or episode.
    """

    def __init__(self, directory: Path, budget: int = SPILL_BYTES):
        if type(budget) is not int or budget < 0:
            raise ValueError("Invalid training byte budget")
        self._directory = tempfile.TemporaryDirectory(dir=directory, prefix="hybrid-training-")
        self._data = tempfile.SpooledTemporaryFile(max_size=max(1, min(budget, SPILL_BYTES)))
        if budget == 0:
            self._data.rollover()
        self._database = sqlite3.connect(Path(self._directory.name) / "index.sqlite")
        self._database.execute("PRAGMA cache_size=-256")
        self._database.execute("PRAGMA journal_mode=OFF")
        self._database.executescript("""
            CREATE TABLE shards (zone TEXT, radar TEXT, side TEXT, offset INTEGER,
                                 size INTEGER, metadata BLOB);
            CREATE INDEX shard_keys ON shards(zone, radar);
            CREATE TABLE episodes (zone TEXT, radar TEXT, state TEXT, source TEXT,
                                   parent TEXT, seconds REAL, weighted REAL,
                                   PRIMARY KEY(zone, radar, state, source, parent));
            CREATE TABLE histograms (zone TEXT, radar TEXT, state TEXT, source TEXT,
                                     channel INTEGER, energy INTEGER, seconds REAL,
                                     PRIMARY KEY(zone,radar,state,source,channel,energy));
        """)
        self._closed = False

    def add(self, group_id, device_id, side, data, metadata):
        if len(data) % CHANNELS or not 0 < len(data) // CHANNELS <= SHARD_CELLS:
            raise ValueError("Invalid training shard shape")
        if len(metadata) > SPILL_BYTES:
            raise ValueError("Oversized training metadata")
        self._data.seek(0, 2)
        offset = self._data.tell()
        self._data.write(data)
        self._database.execute(
            "INSERT INTO shards VALUES (?,?,?,?,?,?)",
            (group_id, device_id, side, offset, len(data), metadata),
        )

    def records(self, group_id, device_id):
        from ...history import pace

        rows = self._database.execute(
            "SELECT side,offset,size,metadata FROM shards WHERE zone=? AND radar=? ORDER BY rowid",
            (group_id, device_id),
        )
        for side, offset, size, metadata in rows:
            pace.rest()
            self._data.seek(offset)
            data = self._data.read(size)
            n = size // CHANNELS
            yield side, tuple(data[c * n : (c + 1) * n] for c in range(CHANNELS)), metadata

    def shards(self, group_id, device_id):
        for _, columns, _ in self.records(group_id, device_id):
            yield columns

    def observe(self, group_id, device_id, lo, hi, span, values=None):
        """Accumulate actual observed time, with one denominator per parent key."""
        if hi <= lo or span.state not in ("present", "not_present"):
            return
        seconds = hi - lo
        key = group_id, device_id, span.state, span.source
        self._database.execute(
            """
            INSERT INTO episodes VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(zone,radar,state,source,parent) DO UPDATE SET
                seconds=seconds+excluded.seconds, weighted=weighted+excluded.weighted
            """,
            (*key, span.episode_id, seconds, seconds * span.confidence),
        )
        if values is not None:
            self._histogram(key, values, seconds)

    def _histogram(self, key, values, seconds):
        self._database.executemany(
            """
            INSERT INTO histograms VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(zone,radar,state,source,channel,energy) DO UPDATE SET
                seconds=seconds+excluded.seconds
            """,
            ((*key, c, energy, seconds) for c, energy in enumerate(values) if energy <= 100),
        )

    def statistics(self, group_id, device_id):
        result = {}
        for state in ("present", "not_present"):
            seconds, weighted, count, mass = self._database.execute(
                """
                SELECT coalesce(sum(seconds),0), coalesce(sum(weighted),0), count(*),
                       coalesce(sum(weighted/seconds),0)
                FROM episodes WHERE zone=? AND radar=? AND state=?
                """,
                (group_id, device_id, state),
            ).fetchone()
            result[state] = {
                "seconds": seconds,
                "weighted_seconds": weighted,
                "episodes": count,
                "episode_mass": mass,
            }
            sources = self._database.execute(
                """SELECT source,sum(seconds),sum(weighted),count(*),sum(weighted/seconds)
                   FROM episodes WHERE zone=? AND radar=? AND state=? GROUP BY source""",
                (group_id, device_id, state),
            )
            result[state]["sources"] = {
                source: {
                    "seconds": seconds,
                    "weighted_seconds": weighted,
                    "episodes": count,
                    "episode_mass": mass,
                }
                for source, seconds, weighted, count, mass in sources
            }
        return result

    def histograms(self, group_id, device_id, channel):
        """Use human class support first; preference mass is unweighted duration."""
        result = {}
        for state in ("present", "not_present"):
            result[state] = self._class_histogram(group_id, device_id, state, channel)
        return result

    def _class_histogram(self, group_id, device_id, state, channel):
        rows = list(
            self._database.execute(
                """
            SELECT source,energy,sum(seconds) FROM histograms
            WHERE zone=? AND radar=? AND state=? AND channel=? GROUP BY source,energy
            """,
                (group_id, device_id, state, channel),
            )
        )
        human = any(source == "human" for source, _, _ in rows)
        bins = [0.0] * 101
        for source, energy, seconds in rows:
            if (source == "human") == human:
                bins[energy] += seconds
        return bins

    def close(self):
        if not self._closed:
            self._closed = True
            geometry = self.__dict__.pop("_timing_geometry", None)
            if geometry is not None:
                geometry.close()
            self._database.close()
            self._data.close()
            self._directory.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

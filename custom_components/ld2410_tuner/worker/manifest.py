"""Job-private, disk-indexed history metadata; never a second history database."""

import datetime
import json
import sqlite3
import stat
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path


def _json(value):
    return json.dumps(value, allow_nan=False, separators=(",", ":"))


class Writer:
    """Spool one prepared view at a time before its protected child starts."""

    def __init__(self, path):
        self.path = Path(path).resolve()

    def __enter__(self):
        self.database = sqlite3.connect(self.path)
        self.database.execute("PRAGMA cache_size=-256")
        self.database.execute("PRAGMA journal_mode=OFF")
        self.database.execute("CREATE TABLE views (name TEXT PRIMARY KEY, metadata TEXT NOT NULL)")
        self.database.execute(
            "CREATE TABLE days (view TEXT, device TEXT, date TEXT, metadata TEXT NOT NULL, "
            "PRIMARY KEY (view,device,date))"
        )
        return self

    def __call__(self, name, request):
        self.database.execute(
            "INSERT INTO views VALUES (?,?)",
            (name, _json({key: value for key, value in request.items() if key != "days"})),
        )
        self.database.executemany(
            "INSERT INTO days VALUES (?,?,?,?)",
            ((name, day["device_id"], day["date"], _json(day)) for day in request["days"]),
        )

    def __exit__(self, kind, value, traceback):
        try:
            if kind is None:
                self.database.commit()
        finally:
            self.database.close()


class _Reader(Mapping):
    def __init__(self, path):
        path = Path(path)
        if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
            raise ValueError("Invalid job manifest path")
        self.path = path.resolve()

    def _query(self, sql, parameters=()):
        # A connection per query: executor threads may read, and the owner must
        # be able to remove the job directory without waiting for collection.
        uri = self.path.as_uri() + "?mode=ro&immutable=1"
        with closing(sqlite3.connect(uri, uri=True, check_same_thread=False)) as database:
            database.execute("PRAGMA cache_size=-256")
            return database.execute(sql, parameters).fetchall()


class Views(_Reader):
    def __iter__(self):
        for (name,) in self._query("SELECT name FROM views ORDER BY name"):
            yield name

    def __len__(self):
        return self._query("SELECT count(*) FROM views")[0][0]

    def __getitem__(self, name):
        rows = self._query("SELECT metadata FROM views WHERE name=?", (name,))
        if not rows:
            raise KeyError(name)
        result = json.loads(rows[0][0])
        result["days"] = {"database": str(self.path), "view": name}
        return result


class DayEntries(_Reader):
    """Read individual day descriptors without materializing an entire manifest."""

    def __init__(self, descriptor):
        super().__init__(descriptor["database"])
        self.view = descriptor["view"]

    def __len__(self):
        return self._query("SELECT count(*) FROM days WHERE view=?", (self.view,))[0][0]

    def __iter__(self):
        for device, date in self._query(
            "SELECT device,date FROM days WHERE view=? ORDER BY device,date", (self.view,)
        ):
            yield device, datetime.date.fromisoformat(date)

    def __getitem__(self, key):
        device, date = key
        rows = self._query(
            "SELECT metadata FROM days WHERE view=? AND device=? AND date=?",
            (self.view, device, date.isoformat()),
        )
        if not rows:
            raise KeyError(key)
        return json.loads(rows[0][0])

    def dates(self, device):
        return [
            datetime.date.fromisoformat(date)
            for (date,) in self._query(
                "SELECT date FROM days WHERE view=? AND device=? ORDER BY date", (self.view, device)
            )
        ]


def expand(request):
    """Expand the small control message into lazy read-only view metadata."""
    return {**request, "views": Views(request["manifest"])}

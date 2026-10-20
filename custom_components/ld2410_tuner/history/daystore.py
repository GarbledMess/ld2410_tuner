"""Recorded history on disk: ``<root>/<radar>/<YYYY-MM-DD>.log`` for the live day and
``<YYYY-MM-DD>.day`` for finished days (UTC).

``append`` runs on the event loop and only touches memory: the live day's builder (what reads
see at once) and a buffer of records not yet written. ``flush`` (executor) appends the buffers
to the logs and finishes every day that has ended into its columnar file. A day starts with
the vector and guess run carried over from the day before, written into its own log, so every
day file is self-contained. ``open`` (executor, at startup) removes leftovers of an interrupted
finish, ends coverage left open by a crash at the last stored record, and finishes old logs.
"""

import datetime
import shutil
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import quote, unquote

from . import dayfile, pace, summaries, summary_cache, worker_snapshot

DAY = datetime.timedelta(days=1)
MIB = 1024 * 1024


def date_of(timestamp):
    return datetime.datetime.fromtimestamp(timestamp, datetime.UTC).date()


def bounds(date):
    start = datetime.datetime(date.year, date.month, date.day, tzinfo=datetime.UTC).timestamp()
    return start, start + DAY.total_seconds()


def _folder_name(device_id):
    return quote(device_id, safe="").replace(".", "%2E")


class _Live:
    """A day still being recorded: its builder, unwritten records and log position."""

    __slots__ = ("date", "builder", "buffer", "written", "last", "seen", "generation")

    def __init__(self, date, records=(), generation=0):
        start, end = bounds(date)
        self.generation = generation
        self.date, self.builder, self.buffer = date, dayfile.DayBuilder(start, end), []
        self.written = self.last = self.seen = start
        for record in records:
            self.builder.add(record)
            self.written = self.last = self.seen = record.time

    def add(self, record):
        self.builder.add(record)
        self.buffer.append(record)
        self.last = record.time
        self.seen = max(self.seen, record.time)

    def is_open(self):
        return self.builder.current is not None or self.builder.run is not None


def _carry_records(carry, start):
    records = []
    if carry.vector is not None:
        records.append(dayfile.Change(start, dict(enumerate(carry.vector))))
    if carry.run is not None:
        records.append(dayfile.Guess(start, *carry.run[2:]))
    return records


class DayStore:
    def __init__(self, root, *, cache_bytes=64 * MIB):
        self.root = Path(root)
        self._live, self._closing = {}, {}
        self._late = {}  # Device id -> {earlier finished date: guess spans recorded later}.
        self._changes = {}  # (device id, date) -> finished-day writes and deletes so far.
        self._cache, self._cached, self._cache_bytes = OrderedDict(), 0, cache_bytes
        self._generations = {}  # Clear invalidates detached flush and compaction writes.
        self._revision = {}  # Device id -> count of finished-day writes, rewrites and deletes.
        self._rewrites = {}  # Device id -> count of rewrites and deletes only.
        self._lock = threading.RLock()
        self._fs_lock = threading.RLock()
        self._pending = {}
        self._clear_pending = {}
        self._publication_error = None

    def folder(self, device_id):
        return self.root / _folder_name(device_id)

    def _path(self, device_id, date, suffix):
        return self.folder(device_id) / f"{date.isoformat()}{suffix}"

    # ----------------------------------------------------------------- startup

    def open(self, now):
        """Recover the folder after a restart (executor)."""
        if not self.root.exists():
            return
        for leftover in self.root.glob("*.tmp"):
            leftover.unlink()  # Interrupted finished-day publication, never physical history.
        for folder in sorted(path for path in self.root.iterdir() if path.is_dir()):
            pace.rest()
            for leftover in (*folder.glob("*.tmp"), *folder.glob("*.trim")):
                leftover.unlink()
            for log in sorted(folder.glob("*.log")):
                pace.rest()
                self._recover(unquote(folder.name), log, date_of(now))

    def _recover(self, device_id, log, today):
        date = datetime.date.fromisoformat(log.stem)
        if self._path(device_id, date, ".day").exists():
            log.unlink()  # The finished file was written; only the log's removal was lost.
            return
        raw = log.read_bytes()
        records = dayfile.decode_log(raw, bounds(date)[0])
        for record in records:
            if isinstance(record, dayfile.GuessSpan):
                self._earlier(device_id, date, record)  # Its earlier days may predate a crash.
        live = _Live(date, records, self._generations.get(device_id, 0))
        if live.is_open():
            end = dayfile.End(live.last)  # A crash: nothing is known after the last record.
            live.add(end)
            records.append(end)
        if date < today:
            self._finish(device_id, live)
        else:
            self._repair_log(log, records, bounds(date)[0], raw)
            live.buffer = []  # The recovered End is already durable before another append.
            self._live[device_id] = live

    @staticmethod
    def _repair_log(log, records, previous, original):
        """Publish only complete records; a partial recovery is retried at next startup."""
        canonical = dayfile.encode_records(records, previous)
        if canonical != original:
            temporary = log.with_name(log.name + ".tmp")
            temporary.write_bytes(canonical)
            temporary.replace(log)

    # ----------------------------------------------------------------- writing

    def append(self, device_id, record):
        """Record ``record`` (event loop; memory only until ``flush``)."""
        date = date_of(record.time)
        with self._lock:
            live = self._live.get(device_id)
            if live is None:
                live = self._live[device_id] = _Live(
                    date, generation=self._generations.get(device_id, 0)
                )
            if date < live.date:
                raise ValueError("Recorded time is before the day being recorded")
            if date > live.date:
                live = self._roll(device_id, live, date)
            if isinstance(record, dayfile.GuessSpan):
                self._earlier(device_id, live.date, record)
            live.add(record)

    def _earlier(self, device_id, date, span):
        """The part of ``span`` before ``date``, kept for those earlier days: reads apply it at
        once and ``flush`` writes it into their finished files. A day still closing is never
        changed (its finish may already have read it); the next flush adds the span."""
        day = date_of(span.start)
        while day < date:
            self._late.setdefault(device_id, {}).setdefault(day, []).append(span)
            day += DAY

    def _roll(self, device_id, live, date):
        """End ``live``'s day and every silent day after it, up to ``date``."""
        while live.date < date:
            self._closing.setdefault(device_id, {})[live.date] = live
            carry = dayfile.carry_of(live.builder.day())
            following = _Live(live.date + DAY, generation=live.generation)
            for record in _carry_records(carry, bounds(following.date)[0]):
                following.add(record)
            live = self._live[device_id] = following
        return live

    def flush(self, now=None):
        """Write buffered records and finish ended days (executor). Returns log bytes written."""
        today = date_of(time.time() if now is None else now)
        with self._lock:
            for device_id, live in list(self._live.items()):
                if live.date < today:
                    self._roll(device_id, live, today)
            pending = [(key, live, live.buffer) for key, live in self._live.items() if live.buffer]
            for _key, live, _records in pending:
                live.buffer = []
            closing = [(key, live) for key, days in self._closing.items() for live in days.values()]
            late, self._late = self._late, {}
            generations = {key: self._generations.get(key, 0) for key in late}
        written = sum(self._write_log(key, live, records) for key, live, records in pending)
        for device_id, live in closing:
            self._finish(device_id, live)
        for device_id, dates in late.items():
            for date, spans in dates.items():
                self._write_late(device_id, date, spans, generations[device_id])
        return written

    def _write_late(self, device_id, date, spans, generation):
        """Splice ``spans`` into the finished ``date`` unless the radar was cleared since they
        were taken (``generation``)."""
        with self._fs_lock:
            if generation != self._generations.get(device_id, 0):
                return
            day = self.day(device_id, date) or dayfile.DayBuilder(*bounds(date)).day()
            spliced = dayfile.splice(day, spans)
            if spliced is not day:
                self._write_day(device_id, date, spliced, generation=generation)

    def _write_log(self, device_id, live, records):
        data = dayfile.encode_records(records, live.written)
        path = self._path(device_id, live.date, ".log")
        with self._fs_lock:
            self._remove_files(device_id)
            if live.generation != self._generations.get(device_id, 0):
                return 0
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("ab") as log:
                log.write(data)
            live.written = records[-1].time
        return len(data)

    def _finish(self, device_id, live):
        day = live.builder.day()
        if len(day.times) or day.guesses:  # A day with no coverage at all keeps no file.
            if not self._write_day(device_id, live.date, day, generation=live.generation):
                return
        with self._fs_lock:
            self._remove_files(device_id)
            if live.generation != self._generations.get(device_id, 0):
                return
            self._path(device_id, live.date, ".log").unlink(missing_ok=True)
        with self._lock:
            self._closing.get(device_id, {}).pop(live.date, None)

    def _write_day(self, device_id, date, day, generation=None):
        with self._lock:
            if generation is None:
                generation = self._generations.get(device_id, 0)
        data = dayfile.encode_day(day)  # Clear never waits behind encoding or the staging write.
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.root, suffix=".tmp", delete=False) as out:
                temporary = Path(out.name)
                out.write(data)
            return self._publish_day(device_id, date, day, temporary, generation)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _publish_day(self, device_id, date, day, temporary, generation):
        with self._fs_lock:
            self._remove_files(device_id)
            if generation != self._generations.get(device_id, 0):
                return False
            path = self._path(device_id, date, ".day")
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.replace(path)
            summary_cache.invalidate(path)
        with self._lock:
            if generation == self._generations.get(device_id, 0):
                self._remember((device_id, date), day)
                self._changed((device_id, date))
                self._revise(device_id)
        return True

    # ----------------------------------------------------------------- reading

    def current(self, device_id):
        """The vector being recorded (None: no open coverage)."""
        with self._lock:
            live = self._live.get(device_id)
            vector = None if live is None else live.builder.current
            return None if vector is None else tuple(vector)

    def run(self, device_id):
        """The open guess run as ``(start, state, level, basis)``, or None."""
        with self._lock:
            live = self._live.get(device_id)
            return None if live is None else live.builder.run

    def live_date(self, device_id):
        """The date being recorded (None: nothing recorded since startup)."""
        with self._lock:
            live = self._live.get(device_id)
            return None if live is None else live.date

    def last(self, device_id):
        """The time of the newest record (None: nothing recorded since startup)."""
        with self._lock:
            live = self._live.get(device_id)
            return None if live is None else live.last

    def mark(self, device_id):
        """Changes whenever the radar's recorded history does: a record, a finished day being
        written, rewritten or deleted. Cheap enough to key caches on every poll."""
        with self._lock:
            live = self._live.get(device_id)
            state = None
            if live is not None:
                builder = live.builder
                state = (live.date, len(builder.times), len(builder.guesses), live.last)
            return self._revision.get(device_id, 0), state

    def observe(self, device_id, timestamp):
        """The radar was observed unchanged at ``timestamp``: memory only, never a record (a
        crash still ends coverage at the last record)."""
        with self._lock:
            live = self._live.get(device_id)
            if live is not None and live.builder.current is not None:
                if date_of(timestamp) > live.date:
                    live = self._roll(device_id, live, date_of(timestamp))
                live.seen = max(live.seen, timestamp)

    def seen(self, device_id):
        """When the radar was last observed (None: nothing recorded since startup)."""
        with self._lock:
            live = self._live.get(device_id)
            return None if live is None else live.seen

    def recording(self):
        """Radars whose coverage or guess run is open."""
        with self._lock:
            return [key for key, live in self._live.items() if live.is_open()]

    def day(self, device_id, date, until=None):
        """The recorded ``date`` (None: nothing recorded). The live day is read as it stands,
        its open coverage ending at ``until`` (default: now)."""
        with self._lock:
            live = self._live.get(device_id)
            if live is not None and live.date == date:
                start, end = bounds(date)
                limit = max(time.time(), live.seen) if until is None else until
                return live.builder.view(min(max(limit, start), end))
            late = list(self._late.get(device_id, {}).get(date, ()))
            pending = self._pending.get((device_id, date))
            if pending is not None:
                return _visible_pending(pending[0], late)
            closing = self._closing.get(device_id, {}).get(date)
            if closing is not None:
                return dayfile.splice(closing.builder.day(), late)
            if device_id in self._clear_pending:
                return None
            cached = self._cache.get((device_id, date))
            if cached is not None:
                self._cache.move_to_end((device_id, date))
                return dayfile.splice(cached, late)
        return self._finished_day(device_id, date, late)

    def _finished_day(self, device_id, date, late):
        path = self._path(device_id, date, ".day")
        if not path.exists():
            return dayfile.splice(dayfile.DayBuilder(*bounds(date)).day(), late) if late else None
        day = dayfile.decode_day(path.read_bytes())
        self._remember((device_id, date), day)
        return dayfile.splice(day, late)

    def worker_snapshot(self, device_id, date, until=None, *, limits=None):
        """Acquire one bounded detached day or finished-file reference (executor only)."""
        return worker_snapshot.acquire(self, device_id, date, until, limits=limits)

    def summary(self, device_id, date, until=None):
        """Acquire a physical index in an executor; generic day reads do no cache I/O.

        Only immutable on-disk days have sidecars. Live and closing builders keep
        their existing in-memory index lifetime and are never persisted here.
        """
        summary_cache.worker_only()
        path = self._path(device_id, date, ".day")
        with self._lock:
            live = self._live.get(device_id)
            mutable = (live is not None and live.date == date) or date in self._closing.get(
                device_id, {}
            )
        try:
            identity = None if mutable else summary_cache.stamp(path)
        except OSError:
            identity = None
        day = self.day(device_id, date, until=until)
        if day is None:
            return None
        if identity is None:
            return summaries.of_day(day)
        loaded = summary_cache.load(day, path, identity=identity)
        if loaded is not None:
            return summaries.remember(day, loaded)
        result = summaries.of_day(day)
        summary_cache.store(result, path, self.root, identity=identity)
        return result

    def days(self, device_id):
        dates = set()
        folder = self.folder(device_id)
        if folder.exists():
            dates.update(
                datetime.date.fromisoformat(path.name[:10])
                for path in folder.iterdir()
                if path.suffix in (".day", ".log")
            )
        with self._lock:
            if device_id in self._live:
                dates.add(self._live[device_id].date)
            dates.update(self._closing.get(device_id, {}))
            dates.update(self._late.get(device_id, {}))
            self._accepted_dates(device_id, dates)
        return sorted(dates)

    def _accepted_dates(self, device_id, dates):
        if device_id in self._clear_pending:
            dates.clear()
            dates.update(self._closing.get(device_id, {}))
            dates.update(self._late.get(device_id, {}))
            if device_id in self._live:
                dates.add(self._live[device_id].date)
        for (key, date), (day, _token) in self._pending.items():
            if key == device_id:
                if _has_data(day):
                    dates.add(date)
                else:
                    dates.discard(date)

    # ----------------------------------------------------------------- maintenance

    def replace(self, device_id, date, day):
        """Install a rewritten finished day (thinning); never the live one."""
        self._refuse_live(device_id, date)
        self._write_day(device_id, date, day)
        self._rewrite(device_id)

    def stage(self, changes):
        """Encode replacements off-loop; visible days stay untouched until commit."""
        staged = []
        try:
            for key, date, day in changes:
                self._refuse_live(key, date)
                path = self._path(key, date, ".day")
                with tempfile.NamedTemporaryFile(
                    dir=path.parent, suffix=".trim", delete=False
                ) as out:
                    temporary = Path(out.name)
                    staged.append((key, date, day, temporary))
                    out.write(dayfile.encode_day(day))
        except BaseException:
            self.discard(staged)
            raise
        return staged

    def commit(self, staged):
        """Publish already encoded days without yielding between validation and rename."""
        with self._fs_lock:
            for key, date, day, temporary in staged:
                path = self._path(key, date, ".day")
                summary_cache.invalidate(path)
                if len(day.times) or day.guesses:
                    temporary.replace(path)
                    self._remember((key, date), day)
                else:
                    path.unlink(missing_ok=True)
                    temporary.unlink(missing_ok=True)
                    self._forget((key, date))
                self._changed((key, date))
                self._revise(key)
                self._rewrite(key)

    @staticmethod
    def discard(staged):
        for _key, _date, _day, temporary in staged:
            temporary.unlink(missing_ok=True)

    def delete(self, device_id, dates):
        with self._fs_lock:
            for date in dates:
                self._refuse_live(device_id, date)
                summary_cache.invalidate(self._path(device_id, date, ".day"))
                for suffix in (".day", ".log"):
                    self._path(device_id, date, suffix).unlink(missing_ok=True)
                self._forget((device_id, date))
                self._changed((device_id, date))
            self._revise(device_id)
            self._rewrite(device_id)

    def _refuse_live(self, device_id, date):
        live = self._live.get(device_id)
        if live is not None and live.date == date:
            raise ValueError("The day being recorded cannot be rewritten")

    def forget_device(self, device_id):
        """Invalidate recordings and queued publications immediately; memory only."""
        with self._lock:
            self._generations[device_id] = self._generations.get(device_id, 0) + 1
            self._clear_pending[device_id] = self._generations[device_id]
            self._live.pop(device_id, None)
            self._closing.pop(device_id, None)
            self._late.pop(device_id, None)
            for key in [key for key in self._pending if key[0] == device_id]:
                self._pending.pop(key)
            for key in [key for key in self._cache if key[0] == device_id]:
                self._forget(key)
            self._revise(device_id)
            self._rewrite(device_id)

    def _remove_files(self, device_id):
        """Drain a requested clear under the filesystem lock, never the recording lock."""
        with self._fs_lock:
            with self._lock:
                generation = self._clear_pending.get(device_id)
            if generation is None:
                return
            try:
                shutil.rmtree(self.folder(device_id))
            except FileNotFoundError:
                pass
            with self._lock:
                if self._clear_pending.get(device_id) == generation:
                    self._clear_pending.pop(device_id, None)

    def remove_device(self, device_id):
        """Synchronous compatibility path; live HA calls use commit.clear_device."""
        self.forget_device(device_id)
        self._remove_files(device_id)

    def reserve(self, staged):
        """Accept replacement views before asynchronous filesystem publication."""
        with self._lock:
            if self._publication_error is not None:
                raise ValueError("History publication failed; restart after restoring disk access")
            token = object()
            entries = []
            for key, date, day, temporary in staged:
                self._refuse_live(key, date)
                if (key, date) in self._pending:
                    raise ValueError("History publication is already pending")
                entries.append((key, date, day, temporary, self._generations.get(key, 0)))
            for key, date, day, _temporary, _generation in entries:
                self._pending[key, date] = day, token
                self._forget((key, date))
                self._changed((key, date))
                self._revise(key)
                self._rewrite(key)
            return token, entries

    def abort_reserved(self, reservation):
        """Withdraw a view when synchronous metadata acceptance failed before any I/O."""
        token, entries = reservation
        with self._lock:
            for key, date, _day, _temporary, _generation in entries:
                if self._pending.get((key, date), (None, None))[1] is token:
                    self._pending.pop((key, date))
                    self._changed((key, date))
                    self._revise(key)
                    self._rewrite(key)

    def publish_reserved(self, reservation):
        """Publish accepted replacements in an executor; failed stages remain recoverable."""
        token, entries = reservation
        try:
            with self._fs_lock:
                for entry in entries:
                    self._publish_reserved_day(token, entry)
        except BaseException as error:
            self._publication_error = error
            raise

    def _publish_reserved_day(self, token, entry):
        key, date, day, temporary, generation = entry
        self._remove_files(key)
        if generation != self._generations.get(key, 0):
            temporary.unlink(missing_ok=True)
            return
        path = self._path(key, date, ".day")
        summary_cache.invalidate(path)
        present = bool(len(day.times) or day.guesses)
        if present:
            temporary.replace(path)
        else:
            path.unlink(missing_ok=True)
            temporary.unlink(missing_ok=True)
        with self._lock:
            if self._pending.get((key, date), (None, None))[1] is token:
                self._pending.pop((key, date))
                if generation == self._generations.get(key, 0) and present:
                    self._remember((key, date), day)

    def _changed(self, key):
        with self._lock:
            self._changes[key] = self._changes.get(key, 0) + 1

    def identity(self, device_id, date):
        """The finished ``date``'s identity: equal while the day is unchanged (None for the
        live or a closing day, a day with guesses still to be written, or no day)."""
        with self._lock:
            live = self._live.get(device_id)
            if (
                (live is not None and live.date == date)
                or date in self._closing.get(device_id, {})
                or date in self._late.get(device_id, {})
                or (device_id, date) in self._pending
                or device_id in self._clear_pending
            ):
                return None
            known = self._generations.get(device_id, 0), self._changes.get((device_id, date), 0)
        try:
            found = self._path(device_id, date, ".day").stat()
        except OSError:
            return None
        return *known, found.st_mtime_ns, found.st_size

    def _revise(self, device_id):
        with self._lock:
            self._revision[device_id] = self._revision.get(device_id, 0) + 1

    def _rewrite(self, device_id):
        with self._lock:
            self._rewrites[device_id] = self._rewrites.get(device_id, 0) + 1

    def rewrites(self, device_id):
        """How often recorded history was rewritten or removed (thinning, Trim, Clear): unlike
        appending and finishing a day, this can change what an earlier view read."""
        with self._lock:
            return self._rewrites.get(device_id, 0)

    def size(self):
        if not self.root.exists():
            return 0
        return sum(
            path.stat().st_size
            for path in self.root.rglob("*")
            if path.is_file() and path.suffix != ".trim"
        )

    # ----------------------------------------------------------------- cache

    def cached_days(self):
        return len(self._cache)

    def _remember(self, key, day):
        with self._lock:
            self._forget(key)
            self._cache[key] = day
            self._cached += _bytes(day)
            while self._cached > self._cache_bytes and len(self._cache) > 1:
                self._forget(next(iter(self._cache)))

    def _forget(self, key):
        with self._lock:
            day = self._cache.pop(key, None)
            if day is not None:
                self._cached -= _bytes(day)


def _bytes(day):
    return 16 * len(day.times) + sum(
        len(column) * column.itemsize if hasattr(column, "itemsize") else len(column)
        for column in day.columns
    )


def _visible_pending(day, late):
    return dayfile.splice(day, late) if len(day.times) or day.guesses or late else None


def _has_data(day):
    return bool(len(day.times) or day.guesses)

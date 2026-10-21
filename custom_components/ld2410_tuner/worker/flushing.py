"""Transactional limited-process journal encoding and finished-day compaction."""

import copy
import datetime
import json
import shutil
import time
from pathlib import Path

from ..history import dayfile, summary_cache
from ..history.daystore import DayStore, date_of
from . import codec, startup

MAX_RECORDS = 131072
MAX_MESSAGE_BYTES = 32 * 1024 * 1024


def _record(record):
    if isinstance(record, dayfile.Change):
        return [0, record.time, list(record.values.items())]
    if isinstance(record, dayfile.End):
        return [1, record.time]
    if isinstance(record, dayfile.Guess):
        return [2, record.time, record.state, record.level, record.basis]
    if isinstance(record, dayfile.GuessSpan):
        return [3, record.time, record.start, record.end, record.state, record.level, record.basis]
    raise ValueError("Unsupported flush record")


def _restore(item):
    kinds = {0: dayfile.Change, 1: dayfile.End, 2: dayfile.Guess, 3: dayfile.GuessSpan}
    if item[0] not in kinds:
        raise ValueError("Unsupported flush record kind")
    fields = [item[1], dict(item[2])] if item[0] == 0 else item[1:]
    return kinds[item[0]](*fields)


def _roll(store, now):
    today = date_of(now)
    if len(store._live) > 128:
        raise ValueError("Flush device budget exceeded")
    if any((today - live.date).days > 128 for live in store._live.values()):
        raise ValueError("Flush date budget exceeded")
    for key, live in list(store._live.items()):
        if live.date < today:
            store._roll(key, live, today)


def _detached(store, now):
    with store._lock:
        _roll(store, now)
        items = [(key, live, False) for key, live in store._live.items() if live.buffer]
        items += [
            (key, live, True) for key, days in store._closing.items() for live in days.values()
        ]
        _check(items, store._late)
        detached = [(key, _copy_live(live, closing), closing) for key, live, closing in items]
        late = copy.deepcopy(store._late)
        ownership = {
            "items": items,
            "counts": [len(live.buffer) for _, live, _ in items],
            "late": late,
            "generations": dict(store._generations),
            "affected": _affected(store),
        }
    return detached, late, ownership


def _copy_live(live, closing):
    if closing:
        return copy.deepcopy(live)
    # An already rolled current day needs only its unwritten record prefix. Its
    # accumulated columns never cross this boundary until the day closes.
    detached = copy.copy(live)
    detached.builder = dayfile.DayBuilder(live.builder.start, live.builder.end)
    detached.buffer = copy.deepcopy(live.buffer)
    return detached


def _check(items, late):
    closing = [live for _, live, finished in items if finished]
    if len(items) > 128 or sum(len(live.builder.times) for live in closing) > 1048576:
        raise ValueError("Flush day/row budget exceeded")
    records = sum(len(live.buffer) for _, live, _ in items)
    records += sum(len(spans) for dates in late.values() for spans in dates.values())
    if records > MAX_RECORDS:
        raise ValueError("Flush pending-record budget exceeded")
    startup._budget({"live": [_descriptor(live) for live in closing]})


def _descriptor(live):
    from array import array

    builder = live.builder
    return {
        "rows": len(builder.times),
        "guesses": len(builder.guesses),
        "confirmed": len(builder.confirmed),
        "floats": [isinstance(column, array) for column in builder.columns],
    }


def capture(store, directory, now):
    """Detach bounded raw columns; original buffers remain owned until publication."""
    directory = Path(directory)
    items, late, ownership = _detached(store, now)
    descriptors = []
    records = []
    for index, (key, live, closing) in enumerate(items):
        descriptor = startup.freeze_live(key, live, directory, index)
        descriptor["closing"] = closing
        descriptors.append(descriptor)
        records.append([_record(record) for record in live.buffer])
    startup._budget({"live": descriptors})
    payload = {
        "records": records,
        "late": {
            key: {
                date.isoformat(): [_record(span) for span in spans] for date, spans in dates.items()
            }
            for key, dates in late.items()
        },
    }
    codec.validate(payload, max_nodes=1048576, max_bytes=MAX_MESSAGE_BYTES)
    raw = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode()
    if len(raw) > MAX_MESSAGE_BYTES:
        raise ValueError("Flush record bytes exceeded")
    (directory / "flush-records.json").write_bytes(raw)
    return {"live": descriptors, "generations": ownership["generations"]}, ownership


def _load(request):
    root, directory = Path(request["root"]), Path(request["directory"])
    metadata = request["flush"]
    startup._budget(metadata)
    path = directory / "flush-records.json"
    if path.stat().st_size > MAX_MESSAGE_BYTES:
        raise ValueError("Flush record bytes exceeded")
    payload = json.loads(path.read_bytes())
    codec.validate(payload, max_nodes=1048576, max_bytes=MAX_MESSAGE_BYTES)
    if len(payload["records"]) != len(metadata["live"]):
        raise ValueError("Flush record count mismatch")
    store = DayStore(root)
    store._generations = metadata["generations"]
    for descriptor, records in zip(metadata["live"], payload["records"], strict=True):
        live = startup.load_live(directory, descriptor)
        live.buffer = [_restore(item) for item in records]
        key = descriptor["device"]
        if descriptor["closing"]:
            store._closing.setdefault(key, {})[live.date] = live
        else:
            store._live[key] = live
    store._late = {
        key: {
            datetime.date.fromisoformat(date): [_restore(item) for item in spans]
            for date, spans in dates.items()
        }
        for key, dates in payload["late"].items()
    }
    return store


def execute(request):
    """Encode journals, compress closing days and splice late guesses in the child."""
    try:
        store = _load(request)
    except (KeyError, TypeError) as error:
        raise ValueError("Invalid flush snapshot") from error
    affected = _affected(store)
    written = store.flush(request.get("flush_now", request["now"]))
    return {
        "written": written,
        "effects": [
            [key, date.isoformat(), suffix, store._path(key, date, suffix).exists()]
            for key, date, suffix in sorted(affected)
        ],
    }


def _affected(store):
    files = {(key, live.date, ".log") for key, live in store._live.items() if live.buffer}
    files.update(
        (key, date, suffix)
        for key, dates in store._closing.items()
        for date in dates
        for suffix in (".log", ".day")
    )
    files.update((key, date, ".day") for key, dates in store._late.items() for date in dates)
    return files


def _publish_file(store, source, key, date, suffix, present):
    target = store._path(key, date, suffix)
    summary_cache.invalidate(store._path(key, date, ".day"))
    if present:
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)
    else:
        target.unlink(missing_ok=True)
    store._forget((key, date))


def _ack(store, ownership, accepted):
    with store._lock:
        for (key, live, closing), count in zip(
            ownership["items"], ownership["counts"], strict=True
        ):
            if key not in accepted or live.generation != store._generations.get(key, 0):
                continue
            if count:
                live.written = live.buffer[count - 1].time
                del live.buffer[:count]
            if closing:
                store._closing.get(key, {}).pop(live.date, None)
        _ack_late(store, ownership, accepted)


def _ack_late(store, ownership, accepted):
    for key, dates in ownership["late"].items():
        if key not in accepted or ownership["generations"].get(key, 0) != store._generations.get(
            key, 0
        ):
            continue
        for date, spans in dates.items():
            _ack_spans(store, key, date, len(spans))


def _ack_spans(store, key, date, count):
    current = store._late.get(key, {}).get(date)
    if current is not None:
        del current[:count]
        if not current:
            store._late[key].pop(date, None)


def _backups(store, effects, directory):
    saved = []
    folder = Path(directory) / "flush-rollback"
    folder.mkdir()
    for index, (key, iso, suffix, _) in enumerate(effects):
        target = store._path(key, datetime.date.fromisoformat(iso), suffix)
        backup = folder / str(index)
        existed = target.exists()
        if existed:
            shutil.copy2(target, backup)
        saved.append((target, backup, existed))
    return saved


def _restore_backups(saved):
    for target, backup, existed in saved:
        if existed:
            backup.replace(target)
        else:
            target.unlink(missing_ok=True)


def _effects(store, result, ownership):
    expected = {(key, date.isoformat(), suffix) for key, date, suffix in ownership["affected"]}
    found = {(key, iso, suffix) for key, iso, suffix, _ in result["effects"]}
    if found != expected or len(found) != len(result["effects"]):
        raise ValueError("Invalid flush publication manifest")
    selected = []
    for effect in result["effects"]:
        key = effect[0]
        store._remove_files(key)
        if ownership["generations"].get(key, 0) == store._generations.get(key, 0):
            selected.append(effect)
    return selected


def _publish_effects(store, effects, directory):
    for key, iso, suffix, present in effects:
        date = datetime.date.fromisoformat(iso)
        source = Path(directory) / "history" / store.folder(key).name / (iso + suffix)
        _publish_file(store, source, key, date, suffix, present)
        if suffix == ".day":
            store._changed((key, date))
            store._revise(key)


def publish(store, result, ownership, directory):
    """Publish under the filesystem lock, then acknowledge only captured prefixes."""
    with store._fs_lock:
        effects = _effects(store, result, ownership)
        saved = _backups(store, effects, directory)
        try:
            _publish_effects(store, effects, directory)
        except BaseException:
            _restore_backups(saved)
            raise
        _inventory(store, effects, ownership)
        accepted = {effect[0] for effect in effects}
        _ack(store, ownership, accepted)
    return result["written"]


def _inventory(store, effects, ownership):
    with store._lock:
        for key, iso, suffix, present in effects:
            if ownership["generations"].get(key, 0) == store._generations.get(key, 0):
                store.inventory(key, datetime.date.fromisoformat(iso), suffix, present)


async def _flush(runtime):
    from . import transactions
    from .supervisor import owned_preparation

    store = runtime._days
    ownership = {}

    def snapshot(directory):
        # A Clear can invalidate an in-flight publisher without waiting for I/O.
        # Its deferred removal must finish before a new generation clones logs.
        with store._lock:
            pending = list(store._clear_pending)
        for key in pending:
            store._remove_files(key)
        now = time.time()
        metadata, captured = capture(store, directory, now)
        ownership.update(captured)
        return {
            "flush": metadata,
            "flush_now": now,
            "_source_paths": [
                str(store._path(key, date, suffix).relative_to(store.root))
                for key, date, suffix in captured["affected"]
            ],
        }

    async def accept(result, directory):
        if runtime._days is not store:
            raise ValueError("Flush store ownership changed")
        return await owned_preparation(
            runtime.hass.async_add_executor_job(publish, store, result, ownership, directory)
        )

    return await transactions.run(runtime, "flush", {}, accept, snapshot=snapshot)


async def flush(runtime, *, locked=False):
    """Production flush route; callers already owning storage may retain that lock."""
    if runtime._days is None or not _pending(runtime._days):
        return 0
    if locked:
        return await _flush(runtime)
    async with runtime._storage_lock:
        return await _flush(runtime) if _pending(runtime._days) else 0


def _pending(store):
    today = date_of(time.time())
    with store._lock:
        return bool(
            store._clear_pending
            or any(store._closing.values())
            or any(store._late.values())
            or any(live.buffer or live.date < today for live in store._live.values())
        )

"""Transactional limited-process journal encoding and finished-day compaction."""

import asyncio
import copy
import datetime
import json
import shutil
import time
from pathlib import Path

from ..history import dayfile, summary_cache
from ..history.daystore import DayStore, date_of
from . import codec, startup

ROLLOVER_DAYS = 16
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


def _roll(store, today, owners):
    """Allocate at most one rollover window; return only newly owned days."""
    remaining = ROLLOVER_DAYS
    items = []
    for key, live in list(owners.items()):
        if not remaining:
            break
        until = min(today, live.date + datetime.timedelta(days=remaining))
        dates = range((until - live.date).days)
        following = store._roll(key, live, until)
        owners[key] = following
        items.extend(
            (key, store._closing[key][live.date + datetime.timedelta(days=n)], True) for n in dates
        )
        remaining -= len(dates)
        if following.buffer:
            items.append((key, following, False))
    return items


def _target(store, now):
    with store._lock:
        today = date_of(now)
        for live in store._live.values():
            live.checkpoint()
        owners = {key: live for key, live in store._live.items() if live.date < today}
        _roll(store, today, owners)
        items = [(key, live, False) for key, live in store._live.items() if live.buffer]
        items += [
            (key, live, True) for key, days in store._closing.items() for live in days.values()
        ]
        return {
            "rollovers": owners,
            "today": today,
            "items": items,
            "counts": [len(live.buffer) for _, live, _ in items],
            "late": {
                key: {date: len(spans) for date, spans in dates.items()}
                for key, dates in store._late.items()
            },
            "generations": dict(store._generations),
        }


class _RecordBudget:
    def __init__(self):
        self.records = 0
        self.values = {}
        self.encoded = {}
        # Reserve container/key overhead before considering individual records.
        self.nodes = 8192
        self.bytes = 65536

    def prefix(self, records, limit):
        count = 0
        for record in records:
            if count >= limit or self.records >= MAX_RECORDS:
                break
            value = _record(record)
            nodes = _nodes(value)
            raw = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
            size = len(raw) + 1
            if self.nodes + nodes > 1048576 or self.bytes + size > MAX_MESSAGE_BYTES:
                break
            self.values[id(record)] = value
            self.encoded[id(record)] = raw
            self.records += 1
            self.nodes += nodes
            self.bytes += size
            count += 1
        return count


def _nodes(value):
    if isinstance(value, (list, tuple)):
        return 1 + sum(_nodes(item) for item in value)
    return 1


def _eligible(store, key, live, closing, count):
    return live.generation == store._generations.get(key, 0) and (closing or count)


def _batch_descriptor(live, compact):
    if compact:
        return _descriptor(live)
    return {"rows": 0, "guesses": 0, "confirmed": 0, "floats": [False] * 18}


def _select_items(store, target, budget):
    items, counts, descriptors = [], [], []
    for (key, live, closing), limit in zip(target["items"], target["counts"], strict=True):
        if not _eligible(store, key, live, closing, limit):
            continue
        count = budget.prefix(live.buffer, limit)
        if limit and not count:
            break
        compact = closing and count == store._flush_lengths[id(live)]
        descriptor = _batch_descriptor(live, compact)
        startup._budget({"live": [descriptor]})
        try:
            startup._budget({"live": descriptors + [descriptor]}, max_items=128)
        except ValueError:
            break
        descriptors.append(descriptor)
        items.append((key, live, compact))
        counts.append(count)
    return items, counts


def _late_ready(store, key, date, items):
    closing = store._closing.get(key, {}).get(date)
    return closing is None or any(live is closing and done for _, live, done in items)


def _late_candidates(store, target, items):
    for key, dates in target["late"].items():
        if target["generations"].get(key, 0) != store._generations.get(key, 0):
            continue
        for date, limit in dates.items():
            # A late rewrite must follow this day's atomic compaction.
            if limit and _late_ready(store, key, date, items):
                yield key, date, limit


def _select_late(store, target, items, budget):
    late = {}
    days = 0
    for key, date, limit in _late_candidates(store, target, items):
        if len(items) + days >= 128:
            break
        spans = store._late.get(key, {}).get(date, [])
        count = budget.prefix(spans, limit)
        if count:
            late.setdefault(key, {})[date] = spans[:count]
            days += 1
    return late


def _select(store, target):
    budget = _RecordBudget()
    items, counts = _select_items(store, target, budget)
    late = _select_late(store, target, items, budget)
    if not items and not late and _target_pending(store, target):
        raise ValueError("Flush pending-record budget exceeded for one record")
    return items, counts, late, budget


def _snapshot_items(store, target):
    items, originals = [], {}
    for (key, live, closing), limit in zip(target["items"], target["counts"], strict=True):
        frozen = copy.copy(live)
        store._flush_lengths[id(frozen)] = len(live.buffer)
        frozen.buffer = live.buffer[: min(limit, MAX_RECORDS)]
        items.append((key, frozen, closing))
        originals[id(frozen)] = live
        if store._closing.get(key, {}).get(live.date) is live:
            store._closing[key][live.date] = frozen
    return items, originals


def _snapshot(store, target):
    """Capture owners under lock; budget and encode their stable prefixes outside it."""
    frozen = copy.copy(store)
    frozen._generations = dict(store._generations)
    frozen._flush_lengths = {}
    frozen._closing = {key: dict(days) for key, days in store._closing.items()}
    frozen._late = {
        key: {
            date: spans[: min(target["late"].get(key, {}).get(date, 0), MAX_RECORDS)]
            for date, spans in dates.items()
        }
        for key, dates in store._late.items()
    }
    selected = dict(target)
    selected["counts"] = list(target["counts"])
    selected["items"], originals = _snapshot_items(frozen, target)
    return frozen, selected, originals


def _detached_budget(store, now, target=None):
    with store._lock:
        if target is None:
            target = _target(store, now)
        frozen, target, originals = _snapshot(store, target)
    items, counts, late, budget = _select(frozen, target)
    detached = [
        (key, _copy_live(live, closing, count), closing)
        for (key, live, closing), count in zip(items, counts, strict=True)
    ]
    items = [(key, originals[id(live)], closing) for key, live, closing in items]
    affected = {(key, live.date, ".log") for key, live, _ in items}
    affected.update((key, live.date, ".day") for key, live, done in items if done)
    affected.update((key, date, ".day") for key, dates in late.items() for date in dates)
    ownership = {
        "items": items,
        "counts": counts,
        "late": late,
        "generations": frozen._generations,
        "affected": affected,
    }
    return detached, late, ownership, budget


def _detached(store, now, target=None):
    return _detached_budget(store, now, target)[:3]


def _copy_live(live, closing, count):
    # Closing builders are immutable; current builders are not transferred.
    # Serialized record copies already belong to the capture budget.
    detached = copy.copy(live)
    if not closing:
        detached.builder = dayfile.DayBuilder(live.builder.start, live.builder.end)
    detached.buffer = live.buffer[:count]
    return detached


def _descriptor(live):
    from array import array

    builder = live.builder
    return {
        "rows": len(builder.times),
        "guesses": len(builder.guesses),
        "confirmed": len(builder.confirmed),
        "floats": [isinstance(column, array) for column in builder.columns],
    }


def capture(store, directory, now, target=None):
    """Detach bounded raw columns; original buffers remain owned until publication."""
    directory = Path(directory)
    items, late, ownership, budget = _detached_budget(store, now, target)
    descriptors = []
    records = []
    for index, (key, live, closing) in enumerate(items):
        descriptor = startup.freeze_live(key, live, directory, index)
        descriptor["closing"] = closing
        descriptor["finished"] = live.date < date_of(now)
        descriptors.append(descriptor)
        records.append([budget.values[id(record)] for record in live.buffer])
    startup._budget({"live": descriptors}, max_items=128)
    payload = {
        "records": records,
        "late": {
            key: {
                date.isoformat(): [budget.values[id(span)] for span in spans]
                for date, spans in dates.items()
            }
            for key, dates in late.items()
        },
    }
    codec.validate(payload, max_nodes=1048576, max_bytes=MAX_MESSAGE_BYTES)
    raw = _capture_bytes(items, late, budget)
    if len(raw) > MAX_MESSAGE_BYTES:
        raise ValueError("Flush record bytes exceeded")
    (directory / "flush-records.json").write_bytes(raw)
    return {"live": descriptors, "generations": ownership["generations"]}, ownership


def _record_bytes(records, budget):
    return b"[" + b",".join(budget.encoded[id(record)] for record in records) + b"]"


def _capture_bytes(items, late, budget):
    """Join already encoded records without traversing them again."""
    records = b",".join(_record_bytes(live.buffer, budget) for _, live, _ in items)
    devices = []
    for key, dates in late.items():
        days = [
            json.dumps(date.isoformat()).encode() + b":" + _record_bytes(spans, budget)
            for date, spans in dates.items()
        ]
        devices.append(json.dumps(key).encode() + b":{" + b",".join(days) + b"}")
    return b'{"records":[' + records + b'],"late":{' + b",".join(devices) + b"}}"


def _load(request):
    root, directory = Path(request["root"]), Path(request["directory"])
    metadata = request["flush"]
    startup._budget(metadata, max_items=128)
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
        if descriptor.get("finished", descriptor["closing"]):
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
    affected = set()
    written = 0
    for descriptor in request["flush"]["live"]:
        key, date = descriptor["device"], datetime.date.fromisoformat(descriptor["date"])
        live = (
            store._closing[key][date]
            if descriptor.get("finished", descriptor["closing"])
            else store._live[key]
        )
        affected.add((key, date, ".log"))
        if descriptor["closing"]:
            store._finish(key, live)
            affected.add((key, date, ".day"))
        elif live.buffer:
            written += store._write_log(key, live, live.buffer)
    late, store._late = store._late, {}
    for key, dates in late.items():
        for date, spans in dates.items():
            store._write_late(key, date, spans, store._generations.get(key, 0))
            affected.add((key, date, ".day"))
    return {
        "written": written,
        "effects": [
            [key, date.isoformat(), suffix, store._path(key, date, suffix).exists()]
            for key, date, suffix in sorted(affected)
        ],
    }


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


async def _flush(runtime, target=None):
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
        metadata, captured = capture(store, directory, now, target)
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

    result = await transactions.run(runtime, "flush", {}, accept, snapshot=snapshot)
    if target is not None:
        _advance(target, ownership)
    return result


def _advance_item(target, key, live, compact, count):
    for index, (_, original, closing) in enumerate(target["items"]):
        if original is live:
            target["counts"][index] -= count
            if compact or not closing and not target["counts"][index]:
                target["items"][index] = (key, live, False)
            return


def _advance(target, ownership):
    for (key, live, compact), count in zip(ownership["items"], ownership["counts"], strict=True):
        _advance_item(target, key, live, compact, count)
    for key, dates in ownership["late"].items():
        for date, spans in dates.items():
            target["late"][key][date] -= len(spans)


def _target_pending(store, target):
    return any(
        (count or closing) and live.generation == store._generations.get(key, 0)
        for (key, live, closing), count in zip(target["items"], target["counts"], strict=True)
    ) or any(
        count and target["generations"].get(key, 0) == store._generations.get(key, 0)
        for key, dates in target["late"].items()
        for count in dates.values()
    )


def _continue_target(store, target):
    with store._lock:
        owners = {
            key: live
            for key, live in target["rollovers"].items()
            if store._live.get(key) is live
            and not live.buffer
            and live.generation == store._generations.get(key, 0)
            and live.date < target["today"]
        }
        items = _roll(store, target["today"], owners)
        target["rollovers"] = owners
        target["items"] = items
        target["counts"] = [len(live.buffer) for _, live, _ in items]
        target["late"] = {}
        return bool(items)


async def _drain(runtime):
    store = runtime._days
    target = await runtime.hass.async_add_executor_job(_target, store, time.time())
    written = 0
    while True:
        written += await _flush(runtime, target)
        with store._lock:
            pending = _target_pending(store, target)
        if not pending:
            pending = await runtime.hass.async_add_executor_job(_continue_target, store, target)
        if not pending:
            return written
        await asyncio.sleep(0)


async def flush(runtime, *, locked=False):
    """Production flush route; callers already owning storage may retain that lock."""
    from ..runtime import history_save

    if runtime._days is None or not _pending(runtime._days):
        history_save.completed(runtime)
        return 0
    if locked:
        written = await _drain(runtime)
    else:
        async with runtime._storage_lock:
            written = await _drain(runtime) if _pending(runtime._days) else 0
    history_save.completed(runtime)
    return written


def _pending(store):
    today = date_of(time.time())
    with store._lock:
        return bool(
            store._clear_pending
            or any(store._closing.values())
            or any(store._late.values())
            or any(_live_pending(live, today) for live in store._live.values())
        )


def _live_pending(live, today):
    return bool(live.buffer or live.date < today or (live.is_open() and live.seen > live.last))

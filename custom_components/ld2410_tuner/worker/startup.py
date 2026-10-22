"""Recover and migrate private history, then transfer raw bounded live columns."""

import copy
import datetime
import os
import shutil
import stat
import struct
from array import array
from pathlib import Path

from ..history import convert, dayfile
from ..history.daystore import DayStore, _Live
from . import codec, recompaction
from .history import _nonblocking

# A full day can contain more than 131k changes or 32k automatic guess runs.
# Bound the transferred buffers and their restored heap, rather than assuming
# that a normal radar records less than once per second.
MAX_DECODED_BYTES = 192 * 1024 * 1024
MAX_BYTES = MAX_DECODED_BYTES
RUN = struct.Struct("<ddBBB")
CONFIRMED = struct.Struct("<dddBBB")
MAX_ROWS = MAX_BYTES // 34  # Minimum raw bytes per valid row, not a sampling-rate cap.
MAX_GUESSES = MAX_BYTES // RUN.size
FIELDS = (*convert.STORED, *convert.UNTIMED, "label_revision")


def execute(request):
    """Called exclusively in the limited child, against a private cloned root."""
    data = {
        "devices": {
            key: {field: device[field] for field in FIELDS if field in device}
            for key, device in request["data"].get("devices", {}).items()
        }
    }
    codec.validate(data, max_nodes=262144, max_bytes=16 * 1024 * 1024)
    data = copy.deepcopy(data)
    days = DayStore(request["root"])
    days.open(request["now"])
    convert.run(data, days, request["now"])
    days.flush(request["now"])  # Persist late-span repair before handing ownership back.
    recompaction.finished_days(days.root)
    patches = _patches(request["data"], data)
    directory = Path(request.get("directory", request["root"]))
    directory.mkdir(parents=True, exist_ok=True)
    _budget(
        {
            "live": [
                _metadata(key, value, index)
                for index, (key, value) in enumerate(days._live.items())
            ]
        }
    )
    live = [
        freeze_live(key, value, directory, index)
        for index, (key, value) in enumerate(days._live.items())
    ]
    result = {"live": live, "patches": patches, "generations": days._generations}
    _budget(result)
    return result


def _patches(original, converted):
    patches = {}
    for key, device in converted["devices"].items():
        before = original["devices"][key]
        if not any(field in before for field in (*convert.STORED, *convert.UNTIMED)):
            continue
        removed = [field for field in (*convert.STORED, *convert.UNTIMED) if field in before]
        updates = {
            field: device[field]
            for field in ("history_cleanup", "label_revision")
            if field in device
        }
        if "history_cleanup" in updates and "last_learning" in before:
            removed.append("last_learning")
        patches[key] = {"remove": removed, "set": updates}
    return patches


def _run_bytes(runs):
    return b"".join(
        RUN.pack(start, end, dayfile.STATES.index(state), level, basis)
        for start, end, state, level, basis in runs
    )


def _confirmed_bytes(runs):
    return b"".join(
        CONFIRMED.pack(at, start, end, dayfile.STATES.index(state), level, basis)
        for at, (start, end, state, level, basis) in runs
    )


def _metadata(device, live, index):
    builder = live.builder
    return {
        "device": device,
        "date": live.date.isoformat(),
        "rows": len(builder.times),
        "guesses": len(builder.guesses),
        "confirmed": len(builder.confirmed),
        "floats": [isinstance(column, array) for column in builder.columns],
        "current": builder.current,
        "run": None if builder.run is None else list(builder.run),
        "written": live.written,
        "last": live.last,
        "seen": live.seen,
        "generation": live.generation,
        "version": builder._summary_version,
        "file": f"live-{index}.bin",
    }


def freeze_live(device, live, directory, index):
    """Write a live builder as raw buffers, never compressed or replayed by HA."""
    builder = live.builder
    metadata = _metadata(device, live, index)
    _budget({"live": [metadata]})
    with (directory / metadata["file"]).open("xb") as stream:
        for chunk in (builder.times, builder.ends, *builder.columns):
            stream.write(chunk)
        stream.write(_run_bytes(builder.guesses))
        stream.write(_confirmed_bytes(builder.confirmed))
    return metadata


def _size(item):
    rows, guesses, confirmed = item["rows"], item.get("guesses", 0), item.get("confirmed", 0)
    if any(type(count) is not int or count < 0 for count in (rows, guesses, confirmed)):
        raise ValueError("Invalid startup handoff count")
    if rows > MAX_ROWS or guesses + confirmed > MAX_GUESSES:
        raise ValueError(
            f"Startup live count budget exceeded: rows={rows} limit={MAX_ROWS}; "
            f"guesses={guesses} confirmed={confirmed} combined_limit={MAX_GUESSES}"
        )
    flags = item.get("floats", [])
    if len(flags) != 18 or any(type(flag) is not bool for flag in flags):
        raise ValueError("Invalid startup column flags")
    size = (
        rows * (16 + sum(4 if flag else 1 for flag in flags))
        + guesses * RUN.size
        + confirmed * CONFIRMED.size
    )
    if size > MAX_BYTES:
        raise ValueError(f"Startup raw file budget exceeded: raw_bytes={size} limit={MAX_BYTES}")
    return size


def _decoded_size(item):
    """Conservative restored heap estimate, not an RSS measurement or OS limit.

    Columns remain packed. Guess records become Python tuples/floats/list slots;
    their binary byte count alone substantially understates the restored heap.
    """
    raw_rows = item["rows"] * (16 + sum(4 if flag else 1 for flag in item["floats"]))
    return (
        (raw_rows * 9 + 7) // 8
        + item.get("guesses", 0) * 192
        + item.get("confirmed", 0) * 256
        + 4096
    )


def _budget(result, *, max_items=None):
    live = result["live"]
    if max_items is not None and len(live) > max_items:
        raise ValueError(
            f"Startup live device budget exceeded: actual={len(live)} limit={max_items}"
        )
    total = sum(_size(item) for item in live)
    rows = sum(item["rows"] for item in live)
    decoded = sum(_decoded_size(item) for item in live)
    if decoded > MAX_DECODED_BYTES:
        raise ValueError(
            f"Startup decoded heap budget exceeded: actual={decoded} limit={MAX_DECODED_BYTES}; "
            f"rows={rows} guesses={sum(item.get('guesses', 0) for item in live)} "
            f"confirmed={sum(item.get('confirmed', 0) for item in live)} raw_bytes={total}"
        )


def _read(directory, item):
    name = item["file"]
    if not isinstance(name, str) or Path(name).name != name or not name.startswith("live-"):
        raise ValueError("Invalid startup handoff path")
    with open(directory / name, "rb", opener=_nonblocking) as stream:
        found = os.fstat(stream.fileno())
        size = _size(item)
        if not stat.S_ISREG(found.st_mode) or found.st_size != size:
            raise ValueError("Invalid startup handoff file size")
        raw = stream.read(size + 1)
    if len(raw) != size:
        raise ValueError("Changed startup handoff file")
    return memoryview(raw)


def _array(kind, raw):
    result = array(kind)
    result.frombytes(raw)
    return result


def load_live(directory, item):
    """Restore one validated raw builder, off the event loop."""
    raw, position = _read(directory, item), 0
    live = _Live(datetime.date.fromisoformat(item["date"]), generation=item["generation"])
    builder = live.builder
    size = item["rows"] * 8
    builder.times = _array("d", raw[:size])
    builder.ends = _array("d", raw[size : 2 * size])
    position = 2 * size
    builder.columns = []
    for floating in item["floats"]:
        size = item["rows"] * (4 if floating else 1)
        chunk = raw[position : position + size]
        builder.columns.append(_array("f", chunk) if floating else bytearray(chunk))
        position += size
    size = item["guesses"] * RUN.size
    builder.guesses = [
        (start, end, dayfile.STATES[state], level, basis)
        for start, end, state, level, basis in RUN.iter_unpack(raw[position : position + size])
    ]
    position += size
    builder.confirmed = [
        (at, (start, end, dayfile.STATES[state], level, basis))
        for at, start, end, state, level, basis in CONFIRMED.iter_unpack(raw[position:])
    ]
    builder.current = item["current"]
    builder.run = None if item["run"] is None else tuple(item["run"])
    builder._summary_version = item["version"]
    live.written, live.last, live.seen = item["written"], item["last"], item["seen"]
    return live


def load(result, directory):
    """Executor-only raw transfer: no record replay, decompression or migration."""
    _budget(result)
    recovered = DayStore(directory)
    for item in result["live"]:
        if item["device"] in recovered._live:
            raise ValueError("Duplicate startup live device")
        recovered._live[item["device"]] = load_live(Path(directory), item)
    recovered._generations = result.get("generations", {})
    return recovered


def write_day(day, directory, index):
    """Write a read-only day using the same bounded raw-column handoff."""
    live = _Live(datetime.datetime.fromtimestamp(day.start, datetime.UTC).date())
    builder = live.builder
    builder.times, builder.ends = day.times, day.ends
    builder.columns, builder.guesses = day.columns, day.guesses
    return freeze_live("", live, Path(directory), index)


def read_day(directory, descriptor):
    """Restore raw day buffers without decompressing history in HA."""
    builder = load_live(Path(directory), descriptor).builder
    return dayfile.Day(
        builder.start, builder.end, builder.times, builder.ends, builder.columns, builder.guesses
    )


def recover_publication(root):
    """Resolve an interrupted directory swap before cloning startup inputs."""
    root = Path(root)
    backup = root.with_name(root.name + ".startup-backup")
    if not backup.exists():
        return
    if root.exists():
        shutil.rmtree(backup)
    else:
        backup.replace(root)


def publish(runtime, owner, result, directory):
    """Publish the recovered clone after validating its complete live handoff."""
    directory = Path(directory)
    recovered = load(result, directory)
    if runtime._days is not owner or owner._live or owner._closing:
        raise ValueError("Startup store ownership changed")
    backup = owner.root.with_name(owner.root.name + ".startup-backup")
    if backup.exists():
        raise ValueError("Unresolved startup publication backup")
    existed = owner.root.exists()
    if existed:
        owner.root.replace(backup)
    try:
        (directory / "history").replace(owner.root)
    except BaseException:
        if existed:
            backup.replace(owner.root)
        raise
    owner._live, owner._generations = recovered._live, recovered._generations
    owner.reconcile()
    _apply_patches(runtime.data, result["patches"])
    if existed:
        shutil.rmtree(backup)


def _apply_patches(data, patches):
    for key, change in patches.items():
        device = data["devices"][key]
        for field in change["remove"]:
            device.pop(field, None)
        device.update(change["set"])


async def initialize(runtime):
    """Recover startup files under the same mandatory process controls as learning."""
    from . import transactions
    from .supervisor import owned_preparation

    owner = runtime._days
    await owned_preparation(runtime.hass.async_add_executor_job(recover_publication, owner.root))

    async def accept(result, directory):
        await owned_preparation(
            runtime.hass.async_add_executor_job(publish, runtime, owner, result, directory)
        )

    await transactions.run(runtime, "startup", runtime.data, accept)

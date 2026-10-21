"""Owned, bounded storage transactions against a private same-filesystem tree."""

import os
import shutil
import stat
import tempfile
import threading
import time
from pathlib import Path

from . import bridge, priority, protocol, storage_values
from .supervisor import Supervisor, owned_preparation

MESSAGE_BYTES = 16 * 1024 * 1024
MESSAGE_NODES = 1024 * 1024
# The store only ever replaces these atomically, so a private hard link cannot
# be written through. Logs are appended in place and must be copied.
LINKED = (".day", ".sum")


def _message(kind, data):
    normalized = storage_values.normalize(data, max_bytes=MESSAGE_BYTES, max_nodes=MESSAGE_NODES)
    return protocol.pack(kind, normalized, max_bytes=MESSAGE_BYTES, max_nodes=MESSAGE_NODES)


def _read(path, kind):
    with Path(path).open("rb") as source:
        raw = source.read(MESSAGE_BYTES + 1)
    return protocol.unpack(raw, kind, max_bytes=MESSAGE_BYTES, max_nodes=MESSAGE_NODES)


def _stamp(found):
    return found.st_dev, found.st_ino, found.st_mtime_ns, found.st_size


def _copy(source, target, expected):
    descriptor = os.open(
        source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    with os.fdopen(descriptor, "rb") as incoming:
        found = os.fstat(incoming.fileno())
        if not stat.S_ISREG(found.st_mode) or _stamp(found) != expected:
            raise ValueError("Storage changed during snapshot")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as outgoing:
            remaining = expected[-1]
            while remaining:
                block = incoming.read(min(1024 * 1024, remaining))
                if not block:
                    raise ValueError("Storage changed during snapshot")
                outgoing.write(block)
                remaining -= len(block)
        if incoming.read(1) or _stamp(os.fstat(incoming.fileno())) != expected:
            raise ValueError("Storage changed during snapshot")


def _regular(root, path):
    relative = path.relative_to(root)
    if len(relative.parts) > 2 or ".." in relative.parts:
        raise ValueError("Storage directory depth budget exceeded")
    for item in (root, path.parent, path):
        if item.is_symlink() or getattr(item, "is_junction", lambda: False)():
            raise ValueError("Storage links are unsupported")
    try:
        found = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISDIR(found.st_mode):
        return None
    if not stat.S_ISREG(found.st_mode):
        raise ValueError("Storage must contain regular files")
    return _stamp(found)


def _files(root, selected):
    paths = root.rglob("*") if selected is None else (root / item for item in selected)
    for path in paths:
        stamp = _regular(root, path)
        if stamp is not None:
            yield path, stamp


def _link(source, target, expected):
    """Share an immutable file's inode; None when this filesystem cannot link."""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target, follow_symlinks=False)
    except OSError:
        return None
    found = target.lstat()
    if not stat.S_ISREG(found.st_mode) or _stamp(found) != expected:
        raise ValueError("Storage changed during snapshot")
    return True


def _space(target, files):
    copied = sum(stamp[-1] for path, stamp in files if path.suffix not in LINKED)
    existing = target
    while not existing.exists():
        existing = existing.parent
    if copied > shutil.disk_usage(existing).free:
        raise ValueError(f"Storage snapshot needs {copied} bytes of free space")


def copy_tree(root, target, selected=None):
    """Clone regular source files; never mutate or follow source links.

    Size is governed by free space for copied bytes, not a file or byte count:
    finished files are hard-linked where the filesystem allows it.
    """
    root, target = Path(root), Path(target)
    files = list(_files(root, selected)) if root.exists() else []
    _space(target, files)
    target.mkdir(parents=True, exist_ok=True)
    for path, stamp in files:
        destination = target / path.relative_to(root)
        if path.suffix not in LINKED or _link(path, destination, stamp) is None:
            _copy(path, destination, stamp)
    return files


def _prepare(holder, root, operation, data, now, snapshot, fs_lock):
    root.parent.mkdir(parents=True, exist_ok=True)
    directory = holder["directory"] = Path(
        tempfile.mkdtemp(prefix=".ld2410-storage-", dir=root.parent)
    )
    request = {
        "operation": operation,
        "root": str(directory / "history"),
        "directory": str(directory),
        "data": data,
        "now": now,
    }
    with fs_lock:
        extra = {} if snapshot is None else snapshot(directory)
        selected = extra.pop("_source_paths", None)
        copy_tree(root, directory / "history", selected)
        request.update(extra)
    path = directory / "request.json"
    path.write_bytes(_message("learn", request))
    return str(path), str(os.getpid()), "storage"


async def run(runtime, operation, data, accept, *, snapshot=None):
    """Hold shared admission through private preparation, child, acceptance and cleanup."""
    service = (
        runtime._final_storage_service
        if getattr(runtime, "_final_storage_service", None) is not None
        else bridge._service(runtime)
    )
    holder = {}

    async def prepare():
        storage_values.validate(data, max_bytes=MESSAGE_BYTES, max_nodes=MESSAGE_NODES)
        return await owned_preparation(
            runtime.hass.async_add_executor_job(
                _prepare,
                holder,
                Path(runtime._days.root),
                operation,
                data,
                time.time(),
                snapshot,
                getattr(runtime._days, "_fs_lock", threading.RLock()),
            )
        )

    async def consume(raw):
        acknowledgement = protocol.unpack(raw, "result")
        if acknowledgement != {"storage": True}:
            raise ValueError("Invalid storage worker acknowledgement")
        directory = holder["directory"]
        result = await owned_preparation(
            runtime.hass.async_add_executor_job(_read, directory / "result.json", "result")
        )
        if "error" in result:
            from ..history.budget import BudgetExceeded

            raise BudgetExceeded(result["error"])
        holder["accepting"] = True
        accepted = await accept(result, directory)
        holder["accepting"] = False
        return accepted

    async def finish():
        if holder.get("accepting"):
            raise OSError(
                f"Storage acceptance failed; recovery files retained at {holder['directory']}"
            )
        if directory := holder.get("directory"):
            await owned_preparation(runtime.hass.async_add_executor_job(shutil.rmtree, directory))

    return await _admit(service, prepare, consume, finish)


def execute(request):
    """Trusted child dispatch; original recording paths are absent from requests."""
    operation = request["operation"]
    if operation == "startup":
        from .startup import execute as calculate
    elif operation == "maintenance":
        from .maintenance import execute as calculate
    elif operation == "flush":
        from .flushing import execute as calculate
    else:
        raise ValueError("Unsupported storage operation")
    try:
        result = calculate(request)
    except ValueError as error:
        from ..history.budget import BudgetExceeded

        if not isinstance(error, BudgetExceeded):
            raise
        result = {"error": str(error)[:2048]}
    Path(request["directory"], "result.json").write_bytes(_message("result", result))
    return protocol.pack("result", {"storage": True})


async def final_save(runtime):
    """Only shutdown's final storage transaction can run after numerical admission closes."""
    runtime._final_storage_service = Supervisor(
        Path(__file__).with_name("entrypoint.py"), deadline=180, stdout_bytes=1024 * 1024
    )
    try:
        await runtime.async_save()
    finally:
        await runtime._final_storage_service.aclose()
        runtime._final_storage_service = None


async def _admit(service, prepare, consume, finish):
    """The serialized storage caller waits without dropping durability work or copying it."""
    return await service.run_when_available(
        prepare, consume=consume, finish=finish, background=priority.is_background()
    )

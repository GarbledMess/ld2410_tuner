"""Worker-only result sidecar maintenance and cleanup of legacy summary indexes.

Optional sidecars never own physical history. Summaries are built only in memory.
"""

import threading

_lock = threading.RLock()
_RESULT = (".res", ".fit")
_CELL = (".cell", ".cols")
_kept = {}  # Root -> bytes of each kind when it was last walked, plus those written since.


def worker_only():
    # Imported here: worker children use this module and never run an event loop.
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError("Physical summary disk access requires an executor")


def stamp(path):
    found = path.stat()
    return found.st_mtime_ns, found.st_size


def _remove(path):
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _cache_files(root):
    files = []
    for path in root.rglob("*"):
        if path.suffix not in (".sum", ".res", ".fit", ".cell", ".cols"):
            continue
        try:
            found = path.stat()
            files.append((found.st_mtime_ns, path, found.st_size))
        except OSError:
            continue
    return files


def disk_bytes(root):
    worker_only()
    return sum(size for _, _, size in _cache_files(root))


def prune(root, *, max_bytes=None, max_cell_bytes=None):
    """Remove all legacy summaries and bound result sidecars, oldest first."""
    from ..engine.hybrid.constants import CELL_CACHE_DISK_BYTES
    from . import result_cache  # Imported here: it imports this module.

    worker_only()
    with _lock:
        # Writers hold this lock while a temporary exists, so these are crash leftovers.
        for temporary in root.rglob("*"):
            if temporary.suffix in (
                ".sum",
                ".sumtmp",
                ".restmp",
                ".celltmp",
                ".colstmp",
                ".fittmp",
            ):
                _remove(temporary)
        files = _cache_files(root)
        result_limit = result_cache.MAX_DISK_BYTES if max_bytes is None else max_bytes
        cell_limit = CELL_CACHE_DISK_BYTES if max_cell_bytes is None else max_cell_bytes
        _kept[root] = {
            _RESULT: _prune_kind(sorted(i for i in files if i[1].suffix in _RESULT), result_limit),
            _CELL: _prune_kind(sorted(i for i in files if i[1].suffix in _CELL), cell_limit),
        }


def _prune_kind(files, limit):
    total = sum(size for _, _, size in files)
    for _, path, size in files:
        if total <= limit:
            break
        if _remove(path):
            total -= size
    return total


def prune_after(root, path):
    """``prune`` after writing the sidecar ``path``, walking the folder only when it must.

    A job writes a sidecar per radar-day and each walk reads every file's size, so the first
    write counts them and the rest add to that count until a budget would be passed.
    """
    from ..engine.hybrid.constants import CELL_CACHE_DISK_BYTES
    from . import result_cache  # Imported here: it imports this module.

    with _lock:
        kept = _kept.get(root)
        kind = _RESULT if path.suffix in _RESULT else _CELL
        limit = result_cache.MAX_DISK_BYTES if kind is _RESULT else CELL_CACHE_DISK_BYTES
        if kept is None or kept[kind] + path.stat().st_size > limit:
            prune(root)
        else:
            kept[kind] += path.stat().st_size


def invalidate(path):
    """Remove an optional sidecar alongside an already-authorized history mutation."""
    for suffix in (".sum", ".res", ".cell", ".celltmp", ".cols", ".colstmp"):
        _remove(path.with_suffix(suffix))

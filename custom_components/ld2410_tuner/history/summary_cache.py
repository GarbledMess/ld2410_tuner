"""Worker-only result sidecar maintenance and cleanup of legacy summary indexes.

Optional sidecars never own physical history. Summaries are built only in memory.
"""

import threading

_lock = threading.RLock()


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
        if path.suffix not in (".sum", ".res"):
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


def prune(root, *, max_bytes=None):
    """Remove all legacy summaries and bound result sidecars, oldest first."""
    from . import result_cache  # Imported here: it imports this module.

    worker_only()
    with _lock:
        # Writers hold this lock while a temporary exists, so these are crash leftovers.
        for temporary in root.rglob("*"):
            if temporary.suffix in (".sum", ".sumtmp", ".restmp"):
                _remove(temporary)
        _prune_kind(
            sorted(item for item in _cache_files(root) if item[1].suffix == ".res"),
            result_cache.MAX_DISK_BYTES if max_bytes is None else max_bytes,
        )


def _prune_kind(files, limit):
    total = sum(size for _, _, size in files)
    for _, path, size in files:
        if total <= limit:
            break
        if _remove(path):
            total -= size


def invalidate(path):
    """Remove an optional sidecar alongside an already-authorized history mutation."""
    for suffix in (".sum", ".res"):
        _remove(path.with_suffix(suffix))

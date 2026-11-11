"""Advisory, bounded per-day statistics for storage maintenance."""

import json
import math
import os
import shutil
import tempfile
import zlib
from pathlib import Path

from ..const import HISTOGRAM_BINS, HISTORY_KEYS

MAX_COMPRESSED = 256 * 1024
MAX_RAW = 1024 * 1024


def valid(seconds):
    return (
        type(seconds) is dict
        and set(seconds) == set(HISTORY_KEYS)
        and all(
            type(seconds[gate]) is dict
            and set(seconds[gate]) == {"present", "not_present"}
            and all(
                type(seconds[gate][state]) is list
                and len(seconds[gate][state]) == HISTOGRAM_BINS
                and all(
                    type(value) in (int, float) and math.isfinite(value) and value >= 0
                    for value in seconds[gate][state]
                )
                for state in ("present", "not_present")
            )
            for gate in HISTORY_KEYS
        )
    )


def load(path, signature):
    """Return validated counts for exactly this file, labels and cutoff, or a cache miss."""
    saved = _read(path, signature, ".count")
    return saved.get("seconds") if saved is not None and valid(saved.get("seconds")) else None


def load_retention(path, signature):
    saved = _read(path, signature, ".ret")
    value = None if saved is None else saved.get("retention")
    if not _valid_retention(value):
        return None
    bounds, ranges = value
    return None if bounds is None else tuple(bounds), tuple(tuple(item) for item in ranges)


def _read(path, signature, suffix):
    try:
        compressed = path.with_suffix(suffix).read_bytes()
        if len(compressed) > MAX_COMPRESSED:
            return None
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, MAX_RAW + 1)
        if len(raw) > MAX_RAW or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            return None
        saved = json.loads(raw)
        if type(saved) is dict and saved.get("signature") == repr(signature):
            return saved
    except (OSError, ValueError, TypeError, zlib.error):
        pass
    return None


def save(path, signature, seconds):
    """Publish only a complete optional sidecar; failure never changes history."""
    if valid(seconds):
        _write(path, signature, ".count", "seconds", seconds)


def save_retention(path, signature, value):
    if _valid_retention(value):
        _write(path, signature, ".ret", "retention", value)


def _write(path, signature, suffix, field, value):
    temporary = None
    try:
        raw = json.dumps({"signature": repr(signature), field: value}).encode()
        if len(raw) > MAX_RAW:
            return
        compressed = zlib.compress(raw, 1)
        if len(compressed) > MAX_COMPRESSED:
            return
        with tempfile.NamedTemporaryFile(
            dir=path.parent, suffix=suffix + "tmp", delete=False
        ) as out:
            temporary = Path(out.name)
            out.write(compressed)
        temporary.replace(path.with_suffix(suffix))
        temporary = None
    except (OSError, ValueError, TypeError, OverflowError):
        pass
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _valid_retention(value):
    if type(value) not in (tuple, list) or len(value) != 2:
        return False
    bounds, ranges = value
    return (
        (bounds is None or _valid_pair(bounds))
        and type(ranges) in (tuple, list)
        and all(_valid_range(item) for item in ranges)
    )


def _valid_pair(value):
    return (
        type(value) in (tuple, list)
        and len(value) == 2
        and all(type(number) in (int, float) and math.isfinite(number) for number in value)
    )


def _valid_range(value):
    return (
        type(value) in (tuple, list)
        and len(value) == 5
        and _valid_pair(value[:2])
        and type(value[2]) is bool
        and type(value[3]) in (int, float)
        and math.isfinite(value[3])
        and (value[4] is None or (type(value[4]) in (int, float) and math.isfinite(value[4])))
    )


def transfer(private_root, original_root):
    """Copy advisory counts after acceptance when the source day is still identical."""
    private_root, original_root = Path(private_root), Path(original_root)
    for source in (*private_root.glob("*/*.count"), *private_root.glob("*/*.ret")):
        temporary = None
        try:
            source_day = source.with_suffix(".day")
            target = original_root / source.relative_to(private_root)
            target_day = target.with_suffix(".day")
            if (
                source.is_symlink()
                or not source_day.is_file()
                or not target_day.is_file()
                or _stamp(source_day) != _stamp(target_day)
            ):
                continue
            with tempfile.NamedTemporaryFile(
                dir=target.parent, suffix=source.suffix + "tmp", delete=False
            ) as out:
                temporary = Path(out.name)
            shutil.copyfile(source, temporary)
            if _stamp(source_day) == _stamp(target_day):
                os.replace(temporary, target)
                temporary = None
        except OSError:
            pass  # An unavailable cache is an exact recount on the next run.
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def _stamp(path):
    found = path.stat()
    return found.st_mtime_ns, found.st_size

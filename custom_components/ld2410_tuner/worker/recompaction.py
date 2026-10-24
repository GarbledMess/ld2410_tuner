"""Upgrade finished files one at a time inside the private startup clone."""

import os
import stat
import tempfile
import zlib

from ..history import dayfile, pace
from ..history.worker_snapshot import FileReference
from . import history


def finished_days(root):
    """Rewrite legacy encodings without returning numerical columns to HA.

    Startup owns this cloned directory until its existing transactional directory
    publication accepts it. Failure therefore leaves the live store untouched.
    """
    for path in root.glob("*/*.day"):
        pace.rest()
        reference = _legacy_reference(path)
        if reference is not None:
            _replace(reference)


def _legacy_reference(path):
    with open(path, "rb", opener=history._nonblocking) as stream:
        found = os.fstat(stream.fileno())
        if not stat.S_ISREG(found.st_mode) or stream.read(4) != dayfile.MAGIC:
            raise history.HistoryRejected("Invalid finished history file")
        # Inspect only one expanded byte; never inflate a whole current day.
        inflater = zlib.decompressobj()
        version = b""
        while not version and (chunk := stream.read(65536)):
            version = inflater.decompress(chunk, 1)
        if not version or version[0] not in dayfile.VERSIONS:
            raise history.HistoryRejected("Invalid finished history version")
        if version == bytes([dayfile.VERSION]):
            return None
        return FileReference(path, history._identity(found))


def _replace(reference):
    day = history.read(reference)  # Bounded decoding and descriptor/path identity checks.
    encoded = dayfile.encode_day(day)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=reference.path.parent, suffix=".recompact", delete=False
        ) as output:
            temporary = reference.path.parent / os.path.basename(output.name)
            output.write(encoded)
        if history._identity(reference.path.stat()) != reference.identity:
            raise history.HistoryRejected("History changed during recompaction")
        temporary.replace(reference.path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

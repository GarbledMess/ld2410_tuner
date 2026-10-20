"""Isolated child script; protection precedes metadata and numerical imports."""

import os
import sys
from pathlib import Path
from types import ModuleType

MAX_BYTES = 1024 * 1024


def _namespace():
    root = Path(__file__).resolve().parent.parent
    package = ModuleType("ld2410_child")
    package.__path__ = [str(root)]
    sys.modules[package.__name__] = package


def main():
    """Read one capped request file and emit exactly one versioned result."""
    _namespace()
    from ld2410_child.worker.limits import Budget, install

    if len(sys.argv) not in (3, 4) or (len(sys.argv) == 4 and sys.argv[3] != "storage"):
        raise ValueError("Expected request path and parent PID")
    install(Budget(512 * 1024 * 1024, 120, 16 * 1024 * 1024), expected_parent=int(sys.argv[2]))
    from ld2410_child.worker import calculations, codec, protocol

    if len(sys.argv) == 4:
        from ld2410_child.worker import transactions

        request = transactions._read(sys.argv[1], "learn")
        sys.stdout.buffer.write(transactions.execute(request))
        sys.stdout.buffer.flush()
        return
    with open(sys.argv[1], "rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    request = protocol.unpack(raw, "learn", max_bytes=MAX_BYTES)
    result = calculations.execute(request)
    sys.stdout.buffer.write(codec.pack_result(result, max_bytes=MAX_BYTES))
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Diagnostic size stays bounded even for malformed paths and exception text.
        message = f"Worker refused: {type(error).__name__}: {error}"[:1000]
        os.write(2, message.encode("utf-8", errors="replace"))
        sys.exit(1)

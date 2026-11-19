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


def _arguments(argv, allowance):
    """``request parent [storage|display] [budget=memory,cpu,file]``. The parent plans the
    budget from the host and recent jobs (``allowance``); without one, the floors apply."""
    extra = list(argv[3:])
    token = extra.pop() if extra and extra[-1].startswith(allowance.PREFIX) else None
    if len(argv) < 3 or len(extra) > 1 or (extra and extra[0] not in ("storage", "display")):
        raise ValueError("Expected request path and parent PID")
    mode = extra[0] if extra else None
    lane = mode or "fit"
    budget = allowance.defaults(lane) if token is None else allowance.parse(token, lane)
    return argv[1], int(argv[2]), mode, budget


def main():
    """Read one capped request file and emit exactly one versioned result."""
    _namespace()
    from ld2410_child.worker import allowance
    from ld2410_child.worker.diagnostics import describe, emit, safely
    from ld2410_child.worker.limits import Budget, idle_disk_priority, install

    path, parent, mode, budget = _arguments(sys.argv, allowance)
    controls = install(
        Budget(
            budget["memory_mib"] * allowance.MIB,
            budget["cpu_seconds"],
            budget["file_mib"] * allowance.MIB,
        ),
        expected_parent=parent,
    )
    if mode is None:
        idle_disk_priority()
    emit("protected", limits=controls)
    from ld2410_child.worker import calculations, codec, protocol

    if mode == "storage":
        from ld2410_child.worker import transactions

        request = transactions._read(path, "learn")
        emit("storage_calculating", **safely(describe, request, Path(path).stat().st_size))
        sys.stdout.buffer.write(transactions.execute(request))
        emit("result_ready")
        sys.stdout.buffer.flush()
        return
    with open(path, "rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    request = protocol.unpack(raw, "learn", max_bytes=MAX_BYTES)
    emit("calculating", **safely(describe, request, len(raw)))
    result = calculations.execute(request)
    emit("result_ready")
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

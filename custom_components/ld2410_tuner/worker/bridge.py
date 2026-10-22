"""Production bridge: one bounded disposable numerical process per runtime."""

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

from ..const import TRAINING_FIELDS
from . import priority, protocol
from .supervisor import Closed, Supervisor, owned_preparation

METADATA_BYTES = 1024 * 1024
METADATA_NODES = 65536


def preflight(runtime):
    """Bound source metadata before legacy view helpers copy it on the HA loop."""
    data = runtime.data
    devices = data.get("devices", {})
    fields = (
        *TRAINING_FIELDS,
        "history_labels",
        "group_labels",
        "histograms",
        "presence_sources",
        "reference_profile",
    )
    selected = {key: {field: item.get(field) for field in fields} for key, item in devices.items()}
    selected["room_inputs"] = {
        key: data.get(key)
        for key in (
            "room_groups",
            "room_labels",
            "room_presence_sources",
            "learning_schedule",
            "storage_settings",
        )
    }
    protocol._check(selected, METADATA_BYTES, METADATA_NODES)


# Read-only display jobs get their own single child, so a chart never waits behind
# learning. Each lane still runs one low-priority, resource-limited child at a time.
DISPLAY = frozenset({"charts", "export_days", "recording_rows"})


def _service(runtime, operation=None):
    if getattr(runtime, "_worker_closed", False):
        raise Closed("Tuner numerical workers are stopped")
    name = "_display_service" if operation in DISPLAY else "_worker_service"
    service = getattr(runtime, name, None)
    if service is None:
        service = Supervisor(
            Path(__file__).with_name("entrypoint.py"), deadline=180, stdout_bytes=METADATA_BYTES
        )
        setattr(runtime, name, service)
    return service


def _build(holder, operation, prepared):
    from . import capture, codec, manifest

    directory = holder["directory"] = Path(tempfile.mkdtemp(prefix="ld2410-worker-"))
    with manifest.Writer(directory / "history.sqlite") as writer:
        capture.prepare_multi(prepared["views"], directory, sink=writer)
    request = {"manifest": str(writer.path)}
    request.update(operation=operation, parameters=codec.encode(prepared["parameters"]))
    path = directory / "request.json"
    with path.open("xb") as stream:
        stream.write(protocol.pack("learn", request))
    return (str(path), str(os.getpid()), *(("display",) if operation in DISPLAY else ()))


async def calculate(runtime, operation, prepare):
    """Admit IDs first; acquire views/files only after admission and own cleanup."""
    service = _service(runtime, operation)
    holder = {}

    async def admitted():
        # Raw views contain only a validated device ID and day bounds, no training copies.
        if operation != "recording_rows":
            preflight(runtime)
        prepared = prepare()
        return await owned_preparation(
            runtime.hass.async_add_executor_job(_build, holder, operation, prepared)
        )

    async def finish():
        if directory := holder.get("directory"):
            await owned_preparation(runtime.hass.async_add_executor_job(shutil.rmtree, directory))

    # Queue lightweight identities before preparing views. Each caller owns its
    # request; saturation delays valid work instead of failing an accepted job.
    background = priority.is_background(operation)
    raw = await service.run_when_available(admitted, finish=finish, background=background)
    from .codec import unpack_result

    return await owned_preparation(runtime.hass.async_add_executor_job(unpack_result, raw))


async def stop(runtime):
    """Close admission and reap children before releasing the integration runtime."""
    runtime._worker_closed = True
    services = [getattr(runtime, name, None) for name in ("_display_service", "_worker_service")]
    await asyncio.gather(*(service.aclose() for service in services if service is not None))


async def device(runtime, operation, device_id, parameters):
    """Acquire a device view only after the global numerical turn is admitted."""
    from ..rooms import attribution

    def prepare():
        return {
            "views": {device_id: attribution.prepared_view(runtime, device_id)},
            "parameters": {"device_id": device_id, **parameters},
        }

    return await calculate(runtime, operation, prepare)

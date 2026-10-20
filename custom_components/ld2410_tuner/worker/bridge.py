"""Production bridge: one bounded disposable numerical process per runtime."""

import os
import shutil
import tempfile
from pathlib import Path

from ..const import TRAINING_FIELDS
from . import protocol
from .supervisor import Closed, Supervisor, owned_preparation

METADATA_BYTES = 1024 * 1024
METADATA_NODES = 65536


def preflight(runtime):
    """Bound source metadata before legacy view helpers copy it on the HA loop."""
    data = runtime.data
    devices = data.get("devices", {})
    if len(devices) > 128 or len(data.get("room_groups", {})) > 64:
        raise ValueError("Tuner worker device/group budget exceeded")
    fields = (
        *TRAINING_FIELDS,
        "history_labels",
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


def _service(runtime):
    if getattr(runtime, "_worker_closed", False):
        raise Closed("Tuner numerical workers are stopped")
    service = getattr(runtime, "_worker_service", None)
    if service is None:
        service = runtime._worker_service = Supervisor(
            Path(__file__).with_name("entrypoint.py"), deadline=180, stdout_bytes=METADATA_BYTES
        )
    return service


def _build(holder, operation, prepared):
    from . import capture, codec

    directory = holder["directory"] = Path(tempfile.mkdtemp(prefix="ld2410-worker-"))
    request = capture.prepare_multi(prepared["views"], directory)
    request.update(operation=operation, parameters=codec.encode(prepared["parameters"]))
    path = directory / "request.json"
    with path.open("xb") as stream:
        stream.write(protocol.pack("learn", request))
    return str(path), str(os.getpid())


async def calculate(runtime, operation, prepare):
    """Admit IDs first; acquire views/files only after admission and own cleanup."""
    service = _service(runtime)
    holder = {}

    async def admitted():
        preflight(runtime)
        prepared = prepare()
        return await owned_preparation(
            runtime.hass.async_add_executor_job(_build, holder, operation, prepared)
        )

    async def finish():
        if directory := holder.get("directory"):
            await owned_preparation(runtime.hass.async_add_executor_job(shutil.rmtree, directory))

    raw = await service.run(admitted, finish=finish)
    from .codec import unpack_result

    return await owned_preparation(runtime.hass.async_add_executor_job(unpack_result, raw))


async def stop(runtime):
    """Close admission and reap children before releasing the integration runtime."""
    runtime._worker_closed = True
    if service := getattr(runtime, "_worker_service", None):
        await service.aclose()


async def device(runtime, operation, device_id, parameters):
    """Acquire a device view only after the global numerical turn is admitted."""
    from ..rooms import attribution

    def prepare():
        return {
            "views": {device_id: attribution.prepared_view(runtime, device_id)},
            "parameters": {"device_id": device_id, **parameters},
        }

    return await calculate(runtime, operation, prepare)

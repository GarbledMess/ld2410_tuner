"""Memory acceptance and owned executor publication of staged day replacements."""

import asyncio

from ..worker.supervisor import owned_preparation


async def publish(runtime, staged, valid, accept):
    """Linearize acceptance on HA's loop, then own all blocking publication work."""
    store = runtime._days
    if not valid():
        await owned_preparation(runtime.hass.async_add_executor_job(store.discard, staged))
        return False
    reservation = None
    try:
        reservation = store.reserve(staged)
        accept()
    except BaseException:
        if reservation is not None:
            store.abort_reserved(reservation)
        await owned_preparation(runtime.hass.async_add_executor_job(store.discard, staged))
        raise
    await owned_preparation(
        runtime.hass.async_add_executor_job(store.publish_reserved, reservation)
    )
    return True


def clear_device(runtime, device_id):
    """Forget immediately; drain physical removal without blocking HA's loop."""
    store = runtime._days
    store.forget_device(device_id)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        store._remove_files(device_id)
        return None

    by_device = getattr(runtime, "_storage_clear_tasks", None)
    if by_device is None:
        by_device = runtime._storage_clear_tasks = {}
    if device_id in by_device:
        return by_device[device_id]

    async def remove():
        try:
            await owned_preparation(
                runtime.hass.async_add_executor_job(store._remove_files, device_id)
            )
        except OSError as error:
            store._publication_error = error
            raise

    task = asyncio.create_task(remove())
    by_device[device_id] = task
    tasks = getattr(runtime, "_storage_io_tasks", None)
    if tasks is None:
        tasks = runtime._storage_io_tasks = set()
    tasks.add(task)

    def finished(done):
        if not done.cancelled() and done.exception() is None:
            tasks.discard(done)
            by_device.pop(device_id, None)

    task.add_done_callback(finished)
    return task


async def drain(runtime):
    """Shutdown retains ownership of clear requests already admitted."""
    tasks = tuple(getattr(runtime, "_storage_io_tasks", ()))
    if tasks:
        await owned_preparation(asyncio.gather(*tasks))

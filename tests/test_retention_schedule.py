"""History thinning runs once a day, not every hour and every restart."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_tuner  # noqa: F401
from tuner_under_test.history import label_counts, recording

DAY = 86_400


def runtime(stamps):
    devices = {
        f"r{i}": {} if stamp is None else {"history_cleanup": {"timestamp": stamp}}
        for i, stamp in enumerate(stamps)
    }
    found = SimpleNamespace(
        data={"devices": devices},
        _cleanup_task=None,
        async_clean_history=AsyncMock(return_value=False),
    )
    found.hass = SimpleNamespace(
        async_create_background_task=lambda target, name: asyncio.create_task(target)
    )
    return found


async def enforce(found, now):
    with (
        patch.object(recording.time, "time", return_value=now),
        patch.object(label_counts, "ensure", AsyncMock()) as ensure,
    ):
        recording._enforce_history_retention(found)
        await found._cleanup_task
    return ensure


def test_a_clean_is_due_only_once_a_day():
    now = time.time()

    async def scenario():
        recent = runtime([now - 3600, now - 7200])
        ensure = await enforce(recent, now)
        recent.async_clean_history.assert_not_awaited()  # Cleaned within the day.
        assert ensure.await_count == 2  # Label counts are still kept ready.
        stale = runtime([now - 3600, now - DAY - 1])
        await enforce(stale, now)
        stale.async_clean_history.assert_awaited_once()
        new = runtime([None])  # Converted or added: never cleaned.
        await enforce(new, now)
        new.async_clean_history.assert_awaited_once()

    asyncio.run(scenario())

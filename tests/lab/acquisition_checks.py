"""Real HA registry/state events through recording, protected flush, startup and charts."""

import asyncio
import json
import sys
import tempfile
import time
import types
from pathlib import Path
from unittest.mock import patch

from attr import evolve
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

package = types.ModuleType("acquisition_probe_tuner")
package.__path__ = [str(Path(__file__).resolve().parents[2] / "custom_components/ld2410_tuner")]
sys.modules[package.__name__] = package
from acquisition_probe_tuner.const import HISTORY_KEYS  # noqa: E402
from acquisition_probe_tuner.history import commit, daystore  # noqa: E402
from acquisition_probe_tuner.runtime import discovery  # noqa: E402
from acquisition_probe_tuner.runtime.coordinator import TunerRuntime  # noqa: E402
from acquisition_probe_tuner.worker import bridge, flushing, startup  # noqa: E402

DEVICE = "acquisition-probe"


async def main():
    with tempfile.TemporaryDirectory(prefix="tuner-acquisition-") as temporary:
        hass = HomeAssistant(temporary)
        dr.async_setup(hass)
        await dr.async_load(hass, load_empty=True)
        await ar.async_load(hass, load_empty=True)
        await er.async_load(hass, load_empty=True)
        registry = er.async_get(hass)
        for key in HISTORY_KEYS:
            entity = f"sensor.acquisition_{key}_energy"
            entry = registry.async_get_or_create(
                "sensor",
                "esphome",
                entity,
                suggested_object_id=entity.removeprefix("sensor."),
            )
            # Only the hardware device identity is synthetic; entity/state APIs
            # and all recording, persistence and chart code are real.
            registry.entities[entry.entity_id] = evolve(entry, device_id=DEVICE)
        data = {"devices": {DEVICE: {"recording_enabled": True}}}
        runtime = TunerRuntime(hass, None, data)
        runtime._days = daystore.DayStore(Path(temporary) / "history")
        runtime._schedule_save = lambda: None
        # Explicitly await production flushes at deterministic checkpoints.
        runtime._schedule_flush = lambda: None
        runtime.refresh_devices(registry)
        runtime.subscribe_state_changes()
        midnight = int(time.time() // 86400) * 86400
        clock = midnight - 20.0

        async def deliver(at, value, keys=HISTORY_KEYS):
            nonlocal clock
            clock = at
            with patch.object(discovery, "_event_time", lambda state: clock):
                for key in keys:
                    hass.states.async_set(f"sensor.acquisition_{key}_energy", str(value))
                await hass.async_block_till_done()
            with patch("time.time", return_value=at):
                runtime.sample_devices()

        try:
            await deliver(midnight - 20, 0)
            await deliver(midnight - 10, 100)
            await deliver(midnight + 10, 100)
            await deliver(midnight + 20, "unavailable", ("g0_move",))
            await deliver(midnight + 30, 0)
            await deliver(midnight + 120, 0)
            with patch("time.time", return_value=midnight + 125):
                assert await flushing.flush(runtime) > 0
            # No End is sent: this models an abrupt process restart.
            runtime.unsub()
            runtime.unsub = None
            runtime._days = daystore.DayStore(runtime._days.root)
            await startup.initialize(runtime)
            dates = runtime._days.days(DEVICE)
            assert len(dates) == 2
            recovered = [runtime._days.day(DEVICE, date) for date in dates]
            assert recovered[0].ends[-1] == midnight
            assert recovered[1].ends[-1] == midnight + 120
            assert recovered[1].vector(0)[0] == 100
            assert any(
                row[0] is None and row[1] == 100
                for row in (recovered[1].vector(i) for i in range(len(recovered[1].times)))
            )
            assert recovered[1].vector(len(recovered[1].times) - 1)[0] == 0
            inventory = await runtime.async_recording_days()
            assert inventory["devices"][0]["id"] == DEVICE
            assert inventory["devices"][0]["days"] == 2
            listing = await runtime.async_recording_days(DEVICE)
            assert listing["days"][0]["date"] == str(dates[-1])
            first_page = await runtime.async_recording_rows(DEVICE, str(dates[-1]), limit=1)
            raw_rows = list(first_page["rows"])
            offset = first_page["next_offset"]
            while offset is not None:
                page = await runtime.async_recording_rows(
                    DEVICE,
                    str(dates[-1]),
                    offset=offset,
                    limit=1,
                    end=first_page["end"],
                    revision=first_page["revision"],
                )
                raw_rows.extend(page["rows"])
                offset = page["next_offset"]
            assert len(raw_rows) == len(recovered[1].times)
            assert raw_rows[-1][1] == midnight + 120
            for index, row in enumerate(raw_rows):
                assert row == [
                    recovered[1].times[index],
                    recovered[1].ends[index],
                    *recovered[1].vector(index),
                ]
            result = await runtime.async_history_series(
                DEVICE, list(HISTORY_KEYS), 1, 1000, midnight + 120, summary=True
            )
            assert result["points"]
            assert min(point["avg"] for point in result["points"]) == 0
            assert max(point["avg"] for point in result["points"]) == 100
            # All gates are 100 for exactly forty seconds. Buckets crossing
            # the steps must contain duration-weighted means, not only 0/100.
            assert (
                abs(
                    sum(point["avg"] * point["observed_seconds"] for point in result["points"])
                    - 4000
                )
                < 1e-6
            )
            assert max(point["t"] for point in result["points"]) == midnight + 120
            print(
                json.dumps(
                    {
                        "dates": [str(date) for date in dates],
                        "rows": [len(day.times) for day in recovered],
                        "summary_points": len(result["points"]),
                        "raw_rows": len(raw_rows),
                        "raw_pages": len(raw_rows),
                        "observed_until": midnight + 120,
                        "passed": True,
                    }
                )
            )
        finally:
            if runtime.unsub:
                runtime.unsub()
            await bridge.stop(runtime)
            await commit.drain(runtime)
            await hass.async_stop()


if __name__ == "__main__":
    asyncio.run(main())

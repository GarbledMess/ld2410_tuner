"""The synthetic home running on the production path, the way Home Assistant would run it.

``Home`` plays Home Assistant: it owns the clock, the registries, states, services and the store,
loads the integration through ``async_setup_entry``, delivers state changes as events, and talks
to the panel's websocket commands. Numerical jobs cross the real worker protocol through
``worker_transport`` (on Windows and in a test, a child process cannot install its limits).
"""

import asyncio
import datetime
import inspect
import time
import types
from pathlib import Path
from unittest.mock import patch

import ha_boundary
import real_shape
from worker_transport import Transport

integration = ha_boundary.load_runtime()

from tuner_under_test.calibration import automatic, device_io, jobs, results  # noqa: E402
from tuner_under_test.const import DOMAIN, HISTORY_KEYS  # noqa: E402
from tuner_under_test.history import commit  # noqa: E402
from tuner_under_test.rooms import application  # noqa: E402
from tuner_under_test.runtime import coordinator  # noqa: E402
from tuner_under_test.worker.supervisor import Supervisor  # noqa: E402

CLEANUP_TASK = "ld2410_tuner history cleanup"


class WebsocketError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


class Clock:
    """The time every part of the integration sees; moves only when a test moves it."""

    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def moved(self, seconds):
        self.now += seconds


class Registry:
    """Home Assistant's entity registry."""

    def __init__(self):
        self.entities = {}
        self.async_get = self.entities.get

    def add(self, entity_id, device_id=None, *, platform="esphome", unique_id=None, name=None):
        self.entities[entity_id] = types.SimpleNamespace(
            entity_id=entity_id,
            device_id=device_id,
            domain=entity_id.split(".", 1)[0],
            platform=platform,
            unique_id=unique_id or entity_id,
            name=name,
            original_name=name,
            disabled_by=None,
        )

    def async_get_entity_id(self, domain, platform, unique_id):
        for entity_id, entry in self.entities.items():
            if (entry.domain, entry.platform, entry.unique_id) == (domain, platform, unique_id):
                return entity_id
        return None


class Hass:
    """What the integration touches of Home Assistant."""

    def __init__(self, config):
        self.data = {}
        self.websocket_commands = {}
        self.entity_registry = Registry()
        self.values = {}
        self.writes = []
        self.state_listeners = []
        self.interval_listeners = []
        self.bus = types.SimpleNamespace(async_listen=lambda *_args: lambda: None)
        self.states = types.SimpleNamespace(get=self.values.get)
        self.services = types.SimpleNamespace(async_call=self._service_call)
        self.http = types.SimpleNamespace(
            register_view=lambda view: None, async_register_static_paths=self._no_paths
        )
        # The store folder keeps Home Assistant's name without its leading dot: tests own it.
        self.config = types.SimpleNamespace(
            path=lambda *parts: str(Path(config, *(part.lstrip(".") for part in parts)))
        )
        self.device_registry = types.SimpleNamespace(async_get=self._device)
        self.area_registry = types.SimpleNamespace(async_list_areas=self._areas)
        self.devices = {}
        self.areas = {}
        self.maintenance_due = asyncio.Event()

    async def _no_paths(self, _paths):
        return None

    def _device(self, device_id):
        return self.devices.get(device_id)

    def _areas(self):
        return [types.SimpleNamespace(id=key, name=name) for key, name in self.areas.items()]

    def async_add_executor_job(self, function, *args):
        return asyncio.to_thread(function, *args)

    def async_create_task(self, target, *_args, **_kwargs):
        return asyncio.create_task(target)

    def async_create_background_task(self, target, name=None, **_kwargs):
        if name == CLEANUP_TASK:
            return asyncio.create_task(self._when_maintenance_is_due(target))
        return asyncio.create_task(target)

    async def _when_maintenance_is_due(self, target):
        """The startup and hourly cleanup waits for its turn; its first thing after a restart
        is hours of day files, so a test decides when that is what it is testing."""
        try:
            await self.maintenance_due.wait()
        except asyncio.CancelledError:
            target.close()
            raise
        await target

    def track_state(self, entity_ids, listener):
        entry = (frozenset(entity_ids), listener)
        self.state_listeners.append(entry)
        return lambda: self.state_listeners.remove(entry)

    def track_interval(self, listener, interval):
        entry = (listener, interval)
        self.interval_listeners.append(entry)
        return lambda: self.interval_listeners.remove(entry)

    async def _service_call(self, domain, service, data, **_kwargs):
        self.writes.append((domain, service, dict(data)))
        if (domain, service) == ("number", "set_value"):
            state = self.values[data["entity_id"]]
            self.values[data["entity_id"]] = types.SimpleNamespace(
                state=str(float(data["value"])), attributes=state.attributes
            )


def _threshold_attributes(radar):
    domain = real_shape.COARSE_DOMAIN if radar == real_shape.COARSE else {}
    return {"min": 0, "max": 100, "step": 1, **domain}


def _add_radar(hass, radar, room):
    registry, values = hass.entity_registry, hass.values
    plain = types.SimpleNamespace
    for name in HISTORY_KEYS:
        gate, kind = name[1], name[3:]
        registry.add(f"sensor.{radar}_g{gate}_{kind}_energy", radar)
        threshold = f"number.{radar}_g{gate}_{kind}_threshold"
        registry.add(threshold, radar)
        values[threshold] = plain(state="50.0", attributes=_threshold_attributes(radar))
    for kind in ("move", "still"):
        entity_id = f"number.{radar}_max_{kind}_distance_gate"
        registry.add(entity_id, radar)
        limit = "4" if radar in real_shape.TEN_CHANNEL_RADARS else "8"
        values[entity_id] = plain(state=limit, attributes={})
    registry.add(f"switch.{radar}_engineering_mode", radar)
    values[f"switch.{radar}_engineering_mode"] = plain(state="on", attributes={})
    hass.devices[radar] = plain(
        id=radar,
        name=radar.replace("_", " ").title(),
        name_by_user=None,
        area_id=room,
    )


def furnish(hass):
    """The home's registries and current readings, as Home Assistant holds them."""
    for room, (name, _count) in real_shape.ROOMS.items():
        hass.areas[room] = name
    hass.areas.update(real_shape.AREAS_WITHOUT_RADARS)
    for radar, room in real_shape.radars().items():
        _add_radar(hass, radar, room)
    for entity_id, name in real_shape.PHONES.items():
        unique = entity_id.split(".", 1)[1]
        hass.entity_registry.add(entity_id, platform="bermuda", unique_id=unique, name=name)
        hass.entity_registry.add(
            f"{entity_id}_range", platform="bermuda", unique_id=f"{unique}_range"
        )
        hass.values[entity_id] = types.SimpleNamespace(
            state="Kitchen",
            attributes={"friendly_name": name, "area_name": "Kitchen", "area_id": "kitchen"},
        )


class Connection:
    def __init__(self):
        self.result, self.error = None, None

    def send_result(self, _message_id, result):
        self.result = result

    def send_error(self, _message_id, code, message):
        self.error = (code, message)


class Home:
    """One running copy of the home."""

    def __init__(self, config):
        self.config = Path(config)
        self.clock = Clock(real_shape.NOW)
        self.transport = Transport()
        self.hass = None

    @property
    def runtime(self):
        return self.hass.data[DOMAIN]

    async def start(self):
        """Home Assistant loads the integration; returns when its background startup is done."""
        self.hass = Hass(self.config)
        furnish(self.hass)
        ha_boundary.reset()
        await integration.async_setup_entry(self.hass, None)
        await self.hass.data[f"{DOMAIN}_startup"]
        await self.settle()

    async def stop(self):
        """Home Assistant unloads the integration: the clean shutdown path."""
        await integration.async_unload_entry(self.hass, None)

    async def settle(self):
        """Let the background work that followed the last action finish."""
        runtime = self.runtime
        for _ in range(100):
            pending = [
                task
                for task in (runtime._engineering_task, *runtime._label_rebuilds.values())
                if task is not None and not task.done()
            ]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    async def save(self):
        await commit.drain(self.runtime)
        await self.runtime.async_save()

    async def ws(self, command, **fields):
        """Send one panel command; the result, or ``WebsocketError`` as the panel would see."""
        connection = Connection()
        handler = self.hass.websocket_commands[f"{DOMAIN}/{command}"]
        message = {"id": 1, "type": f"{DOMAIN}/{command}"}
        for name in handler.schema:
            if getattr(name, "optional", False) and name not in fields:
                fields[name] = name.default
        message.update(fields)
        result = handler(self.hass, connection, message)
        if inspect.isawaitable(result):
            await result
        if connection.error:
            raise WebsocketError(*connection.error)
        return connection.result

    def report(self, radar, name, value, *, at=None):
        """A radar's sensor changes: Home Assistant fires the state change event."""
        moment = self.clock() if at is None else at
        gate, kind = name[1], name[3:]
        entity_id = f"sensor.{radar}_g{gate}_{kind}_energy"
        state = types.SimpleNamespace(
            state=str(value),
            attributes={},
            last_updated=datetime.datetime.fromtimestamp(moment, datetime.UTC),
        )
        self.hass.values[entity_id] = state
        event = types.SimpleNamespace(data={"entity_id": entity_id, "new_state": state})
        for entity_ids, listener in list(self.hass.state_listeners):
            if entity_id in entity_ids:
                listener(event)

    async def poll(self):
        """Home Assistant's two-second timer: the integration samples, then flushes its logs."""
        await self.runtime.async_sample_devices()

    async def flushed(self):
        """The day logs have been written, or the attempt has failed."""
        task = self.runtime._flush_task
        if task is not None:
            await task

    def thresholds(self, radar):
        return {
            name: float(self.hass.values[f"number.{radar}_g{name[1]}_{name[3:]}_threshold"].state)
            for name in real_shape.channels(radar)
        }

    async def learn(self, radar):
        """Press Learn on a radar and wait for the job, as the panel polls it."""
        job = await self.ws("start_learning", device_id=radar)
        await asyncio.gather(*self.runtime._learning_jobs.values(), return_exceptions=True)
        await asyncio.gather(*self.runtime._room_learning_jobs.values(), return_exceptions=True)
        return job, self.runtime.data["devices"][radar]["learning_job"]


def run_on_clock(clock, coroutine_factory):
    """Run an async scenario with the home's clock standing in for ``time.time``."""
    patches = [
        patch.object(time, "time", clock),
        patch.object(automatic, "time", clock),
        patch.object(jobs, "time", clock),
        patch.object(results, "time", clock),
        patch.object(application, "time", clock),
        patch.object(device_io, "SETTLE_SECONDS", 0),
        patch.object(coordinator, "STORE_DELAY", 0.01),
    ]
    for item in patches:
        item.start()
    try:
        return asyncio.run(coroutine_factory())
    finally:
        for item in reversed(patches):
            item.stop()


async def lifetime(home, scenario):
    """Load, ``scenario``, unload: one run of Home Assistant over this config folder."""
    await home.start()
    try:
        await scenario(home)
    finally:
        await home.stop()


def open_home(template, folder, *, whole=True):
    """A private running-ready copy of the built home under ``folder``."""
    return Home(real_shape.open_copy(template, folder, whole=whole))


def worker_on_transport(home):
    """Route the numerical worker through the real protocol, in this process."""

    async def process(_service, args):
        return await home.transport.process(args)

    return patch.object(Supervisor, "_process", process)

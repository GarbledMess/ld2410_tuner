"""The Home Assistant boundary: the integration package loaded against small stand-ins.

Only what Home Assistant itself provides is replaced (registries, states, services, the store,
websocket registration). Everything the integration does runs for real.
"""

import importlib.util
import json
import os
import sys
import types
from functools import cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "ld2410_tuner"
STUBBED = (
    "voluptuous",
    "homeassistant",
    "homeassistant.components",
    "homeassistant.components.websocket_api",
    "homeassistant.components.frontend",
    "homeassistant.components.http",
    "homeassistant.config_entries",
    "homeassistant.core",
    "homeassistant.helpers",
    "homeassistant.helpers.entity_registry",
    "homeassistant.helpers.device_registry",
    "homeassistant.helpers.area_registry",
    "homeassistant.helpers.event",
    "homeassistant.helpers.storage",
    "homeassistant.util",
    "homeassistant.util.dt",
    "homeassistant.components.http.auth",
    "aiohttp",
    "aiohttp.web",
)


class Store:
    """Home Assistant's JSON store: one file, written atomically, loaded by version."""

    def __init__(self, hass, version, name, *, encoder=None, minor_version=1, **_ignored):
        self.hass, self.version, self.name = hass, version, name
        self.minor_version, self.encoder = minor_version, encoder
        self.path = Path(hass.config.path("storage", name))

    async def async_load(self):
        if not self.path.exists():
            return None
        saved = json.loads(self.path.read_text(encoding="utf8"))
        data = saved["data"]
        if saved["version"] != self.version or saved.get("minor_version") != self.minor_version:
            data = await self._async_migrate_func(
                saved["version"], saved.get("minor_version"), data
            )
        return data

    async def async_save(self, data):
        text = json.dumps(
            {
                "version": self.version,
                "minor_version": self.minor_version,
                "key": self.name,
                "data": data,
            },
            cls=self.encoder,
            separators=(",", ":"),
        )
        await self.hass.async_add_executor_job(self._write, text)

    def _write(self, text):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        partial = self.path.with_suffix(".partial")
        partial.write_text(text, encoding="utf8")
        os.replace(partial, self.path)


def _voluptuous(vol):
    """Schema markers that keep what the integration declares, defaults included."""

    class Marker(str):
        default = None
        optional = False

    def required(name, **_ignored):
        return Marker(name)

    def optional(name, default=None):
        marker = Marker(name)
        marker.optional, marker.default = True, default
        return marker

    vol.Required, vol.Optional = required, optional
    vol.In = vol.Any = vol.All = lambda *values: values
    vol.Coerce = lambda value: value
    vol.Range = lambda **limits: limits


def _websocket(api):
    def command(schema):
        def decorate(function):
            function.schema = schema
            return function

        return decorate

    def register(hass, function):
        hass.websocket_commands[function.schema["type"]] = function

    def require_admin(function):
        function.admin_only = True
        return function

    api.websocket_command = command
    api.async_response = lambda function: function
    api.require_admin = require_admin
    api.async_register_command = register


def _http():
    web = sys.modules["aiohttp"].web = sys.modules["aiohttp.web"]

    class FileResponse:
        def __init__(self, path, *, headers=None, **_ignored):
            self.path, self.headers = Path(path), dict(headers or {})

    web.FileResponse = FileResponse
    web.HTTPNotFound = type("HTTPNotFound", (Exception,), {"status": 404})
    web.HTTPForbidden = type("HTTPForbidden", (Exception,), {"status": 403})
    http = sys.modules["homeassistant.components.http"]
    http.StaticPathConfig = lambda *args, **_ignored: args
    http.HomeAssistantView = type("HomeAssistantView", (), {})
    sys.modules["homeassistant.components.http.auth"].async_sign_path = (
        lambda hass, path, expiration, **_ignored: (
            f"{path}?authSig=signed-{expiration.total_seconds():g}"
        )
    )


def _frontend(frontend):
    panels = set()
    frontend.async_panel_exists = lambda hass, name: name in panels
    frontend.async_register_built_in_panel = lambda hass, **kwargs: panels.add(
        kwargs["frontend_url_path"]
    )
    frontend.async_remove_panel = lambda hass, name: panels.discard(name)


def _core(core):
    def callback(function):
        # Like HA: only marked callables run on the event loop; others go to an executor.
        function._hass_callback = True
        return function

    core.HomeAssistant = object
    core.callback = callback


def _registries():
    """Registries a test's ``hass`` provides; bare defaults for code run without one."""
    modules = sys.modules
    modules["homeassistant.helpers.entity_registry"].EVENT_ENTITY_REGISTRY_UPDATED = "registry"
    modules["homeassistant.helpers.entity_registry"].async_get = lambda hass: getattr(
        hass, "entity_registry", None
    )
    modules["homeassistant.helpers.area_registry"].async_get = lambda hass: getattr(
        hass, "area_registry", types.SimpleNamespace(async_list_areas=lambda: [])
    )
    modules["homeassistant.helpers.device_registry"].async_get = lambda hass: getattr(
        hass, "device_registry", types.SimpleNamespace(async_get=lambda device_id: None)
    )


def _events(events):
    def track_state(hass, entity_ids, listener):
        tracker = getattr(hass, "track_state", None)
        return tracker(entity_ids, listener) if tracker else lambda: None

    def track_interval(hass, listener, interval):
        tracker = getattr(hass, "track_interval", None)
        return tracker(listener, interval) if tracker else lambda: None

    events.async_track_state_change_event = track_state
    events.async_track_time_interval = track_interval


@cache
def _time_zone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def reset():
    """Undo what a test left in the shared stand-ins, for one that runs on a test home."""
    _voluptuous(sys.modules["voluptuous"])
    _websocket(sys.modules["homeassistant.components.websocket_api"])
    _registries()
    _events(sys.modules["homeassistant.helpers.event"])


def load_runtime():
    """The integration package as ``tuner_under_test`` (shared: importing twice is one load)."""
    if "tuner_under_test" in sys.modules:
        return sys.modules["tuner_under_test"]
    for name in STUBBED:
        sys.modules[name] = types.ModuleType(name)
    _http()
    sys.modules["homeassistant.config_entries"].ConfigEntry = object
    _voluptuous(sys.modules["voluptuous"])
    _websocket(sys.modules["homeassistant.components.websocket_api"])
    _frontend(sys.modules["homeassistant.components.frontend"])
    _core(sys.modules["homeassistant.core"])
    _registries()
    _events(sys.modules["homeassistant.helpers.event"])
    sys.modules["homeassistant.helpers.storage"].Store = Store
    sys.modules["homeassistant.util.dt"].get_time_zone = _time_zone
    spec = importlib.util.spec_from_file_location(
        "tuner_under_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    # Tests run background work unthrottled.
    sys.modules["tuner_under_test.history.pace"].SHARE = 1.0
    return module

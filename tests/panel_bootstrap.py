"""Expose the real panel registration to the browser regression runner."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from test_tuner import mod


async def panel_registration():
    """Start and stop the integration with only Home Assistant boundaries stubbed."""
    hass = SimpleNamespace(
        data={},
        bus=SimpleNamespace(async_listen=Mock(return_value=Mock())),
        http=SimpleNamespace(async_register_static_paths=AsyncMock()),
        async_add_executor_job=asyncio.to_thread,
        async_create_task=asyncio.create_task,
    )
    store = SimpleNamespace(
        async_load=AsyncMock(return_value={"devices": {}}), async_save=AsyncMock()
    )
    with (
        patch.object(mod, "TunerStore", return_value=store),
        patch.object(mod, "StaticPathConfig") as static,
        patch.object(mod, "_register_websocket_commands"),
        patch.object(mod.er, "async_get", return_value=SimpleNamespace(entities={}), create=True),
        patch.object(mod.frontend, "async_panel_exists", return_value=False, create=True),
        patch.object(mod.frontend, "async_register_built_in_panel", create=True) as register,
    ):
        # StaticPathConfig accepts positional paths; retain them for browser routing.
        static.side_effect = lambda url, directory, **kwargs: SimpleNamespace(
            url_path=url, path=directory, **kwargs
        )
        await mod.async_setup_entry(hass, None)
        try:
            paths = hass.http.async_register_static_paths.call_args.args[0]
            return {
                "panel": register.call_args.kwargs["config"]["_panel_custom"],
                "static_url": paths[0].url_path,
            }
        finally:
            await mod.async_unload_entry(hass, None)


if __name__ == "__main__":
    print(json.dumps(asyncio.run(panel_registration())))

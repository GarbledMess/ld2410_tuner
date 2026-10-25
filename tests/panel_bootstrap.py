"""Expose the real panel registration to the browser regression runner."""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from test_tuner import mod


def fake_hass(config):
    """Only the Home Assistant boundaries the integration touches at setup."""
    return SimpleNamespace(
        data={},
        config=SimpleNamespace(path=lambda *parts: str(Path(config, *parts))),
        bus=SimpleNamespace(async_listen=Mock(return_value=Mock())),
        http=SimpleNamespace(async_register_static_paths=AsyncMock(), register_view=Mock()),
        async_add_executor_job=asyncio.to_thread,
        async_create_task=asyncio.create_task,
        async_create_background_task=lambda target, name: asyncio.create_task(target),
    )


async def panel_registration():
    """Start and stop the integration with only Home Assistant boundaries stubbed."""
    with TemporaryDirectory(prefix="ld2410-panel-") as config:
        hass = fake_hass(config)
        store = SimpleNamespace(
            async_load=AsyncMock(return_value={"devices": {}}), async_save=AsyncMock()
        )
        with (
            patch.object(mod, "TunerStore", return_value=store),
            patch.object(mod, "StaticPathConfig") as static,
            patch.object(mod, "_register_websocket_commands"),
            patch.object(
                mod.er, "async_get", return_value=SimpleNamespace(entities={}), create=True
            ),
            patch.object(mod.frontend, "async_panel_exists", return_value=False, create=True),
            patch.object(mod.frontend, "async_register_built_in_panel", create=True) as register,
        ):
            # StaticPathConfig accepts positional paths; retain them for browser routing.
            static.side_effect = lambda url, directory, **kwargs: SimpleNamespace(
                url_path=url, path=directory, **kwargs
            )
            await mod.async_setup_entry(hass, None)
            try:
                startup = hass.data.get(f"{mod.DOMAIN}_startup")
                if startup is not None:
                    await startup
                paths = hass.http.async_register_static_paths.call_args.args[0]
                return {
                    "panel": register.call_args.kwargs["config"]["_panel_custom"],
                    "static_url": paths[0].url_path,
                }
            finally:
                await mod.async_unload_entry(hass, None)


if __name__ == "__main__":
    print(json.dumps(asyncio.run(panel_registration())))

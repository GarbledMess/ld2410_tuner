"""A radar's JSON export is written to a file and downloaded from a signed, admin-only URL.

Its day files can be megabytes (weeks of history), far more than one websocket result or the
worker's result pipe carries, so the browser downloads the file instead.
"""

import asyncio
import base64
import json
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import test_surfaces as surfaces
import test_tuner as harness

mod = harness.mod
DAY = 86400
web = sys.modules["aiohttp.web"]


class ExportFileTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown
    configuration = harness.RuntimeTests.configuration
    add_entity = surfaces.SurfaceTests.add_entity
    registry_device = surfaces.SurfaceTests.registry_device

    def record_two_days(self):
        rows = [
            (start + i * 6, bytes([20 + i % 3] + [255] * 17))
            for start in (self.now - DAY - 600, self.now - 600)
            for i in range(50)
        ]
        harness.record_rows(self.runtime, "a", rows)
        self.runtime._days.flush(now=self.now)

    def token(self, result):
        prefix = "/api/ld2410_tuner/export/"
        assert result["url"].startswith(prefix)
        return result["url"][len(prefix) :].split("?")[0]

    def path(self, result):
        folder = Path(self.hass.config.path(".storage", "ld2410_tuner", "exports"))
        return folder / f"{self.token(result)}.json"

    def view(self):
        from tuner_under_test.presentation import export_files

        self.hass.data = {mod.DOMAIN: self.runtime}
        return export_files.ExportView(self.hass)

    def request(self, admin=True):
        return {"hass_user": types.SimpleNamespace(is_admin=admin)}

    async def test_the_file_holds_every_recorded_day_however_large(self):
        from tuner_under_test.history import dayfile, spans
        from tuner_under_test.worker import bridge

        self.registry_device()
        self.record_two_days()
        loop_thread, written = threading.get_ident(), []
        real = dayfile.encode_day

        def encode(day):
            written.append(threading.get_ident() != loop_thread)
            return real(day)

        # Never through the worker's result pipe (1 MiB: a few days of a busy radar).
        with (
            patch.object(bridge, "calculate", side_effect=AssertionError("worker pipe")),
            patch.object(dayfile, "encode_day", side_effect=encode),
        ):
            result = await self.runtime.async_export_file("a")
        assert "authSig=signed" in result["url"]
        assert result["filename"] == "ld2410-tuner-a.json"
        content = json.loads(self.path(result).read_text(encoding="utf-8"))
        assert result["bytes"] == self.path(result).stat().st_size
        assert content["format"] == "ld2410_tuner_export_v2"
        days = content["devices"]["a"]["history"]["day_files"]
        assert len(days) == 2
        decoded = [dayfile.decode_day(base64.b64decode(data)) for data in days.values()]
        found = [(c.start, c.end, c.values) for d in decoded for c in spans.day_spans(d)]
        assert found == [(c.start, c.end, c.values) for c in spans.events(self.runtime, "a")]
        assert all(written), "days are encoded off the event loop"

    async def test_the_statistics_export_leaves_out_day_files(self):
        self.registry_device()
        self.record_two_days()
        exported = await self.runtime.async_export_data("a")
        assert "day_files" not in exported["devices"]["a"]["history"]
        assert exported["devices"]["a"]["sample_counts"] is not None

    async def test_one_radar_exports_whatever_the_other_radars_hold(self):
        self.registry_device()
        # A home's radars together exceed one export's metadata budget; one radar does not.
        self.runtime.data["devices"]["big"] = {"last_learning": {"runs": [0] * 40000}}
        assert "a" in (await self.runtime.async_export_data("a"))["devices"]
        assert self.path(await self.runtime.async_export_file("a")).exists()
        refused = self.runtime.async_export_file("big")
        with pytest.raises(ValueError, match="budget"):
            await refused

    async def test_only_an_admin_downloads_the_current_export(self):
        self.registry_device()
        self.record_two_days()
        result = await self.runtime.async_export_file("a")
        view, token = self.view(), self.token(result)
        assert view.requires_auth
        assert view.url == "/api/ld2410_tuner/export/{token}"
        response = await view.get(self.request(), token)
        assert response.path == self.path(result)
        assert response.headers["Content-Disposition"] == (
            'attachment; filename="ld2410-tuner-a.json"'
        )
        user = self.request(admin=False)
        with pytest.raises(web.HTTPForbidden):
            await view.get(user, token)
        admin = self.request()
        for unknown in ("0" * 32, "../exports", ""):
            with pytest.raises(web.HTTPNotFound):
                await view.get(admin, unknown)

    async def test_a_new_export_replaces_the_previous_file(self):
        self.registry_device()
        first = await self.runtime.async_export_file("a")
        second = await self.runtime.async_export_file("a")
        assert not self.path(first).exists()
        assert self.path(second).exists()
        view, admin, replaced = self.view(), self.request(), self.token(first)
        with pytest.raises(web.HTTPNotFound):
            await view.get(admin, replaced)

    async def test_an_export_is_removed_when_its_link_expires(self):
        from tuner_under_test.presentation import export_files

        self.registry_device()
        with patch.object(export_files, "EXPIRY_SECONDS", 0):
            result = await self.runtime.async_export_file("a")
        assert "authSig=signed-0" in result["url"]  # The link expires with the file.
        for _ in range(10):  # The expiry timer is due at once; it removes the file.
            if (cleanup := getattr(self.runtime, "_export_cleanup", None)) is not None:
                break
            await asyncio.sleep(0)
        await cleanup
        assert not self.path(result).exists()
        view, admin, expired = self.view(), self.request(), self.token(result)
        with pytest.raises(web.HTTPNotFound):
            await view.get(admin, expired)

    async def test_unload_and_startup_remove_exports(self):
        from tuner_under_test.presentation import export_files

        self.registry_device()
        result = await self.runtime.async_export_file("a")
        with (
            patch.object(mod.engineering, "stop", AsyncMock()),
            patch.object(mod.schedule, "stop", AsyncMock()),
        ):
            await mod._shutdown(self.runtime)
        assert not self.path(result).exists()
        leftover = self.path(result)
        leftover.parent.mkdir(parents=True, exist_ok=True)
        leftover.write_text("{}", encoding="utf-8")  # As after a power cut.
        await export_files.clear(self.hass)
        assert not leftover.exists()

    async def test_a_busy_or_failed_export_writes_no_file(self):
        from tuner_under_test.presentation import export_files
        from tuner_under_test.worker.supervisor import Busy

        self.registry_device()
        self.runtime._export_active = True
        busy = self.runtime.async_export_file("a")
        with pytest.raises(Busy):
            await busy
        self.runtime._export_active = False
        with patch.object(export_files, "_write", side_effect=OSError("disk full")):
            failing = self.runtime.async_export_file("a")
            with pytest.raises(OSError):
                await failing
        folder = Path(self.hass.config.path(".storage", "ld2410_tuner", "exports"))
        assert not folder.exists() or not any(folder.iterdir())
        assert not self.runtime._export_active
        unknown = self.runtime.async_export_file("missing")
        with pytest.raises(ValueError, match="Unknown radar"):
            await unknown


def test_a_radar_without_a_day_store_exports_no_day_files():
    from tuner_under_test.presentation import export_files

    assert export_files.day_files(None, "a") == {}

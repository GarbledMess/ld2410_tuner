"""A radar's JSON export as a file, downloaded from a signed, admin-only URL.

Weeks of a radar's day files are megabytes: more than one websocket result or the worker's
result pipe carries. The file is written in the executor; finished days are copied as stored,
so only the day being recorded is encoded. One export is kept, until the next export, unload or
its link's expiry, when the file is removed.
"""

import asyncio
import base64
import json
import re
import secrets
import shutil
from datetime import timedelta
from functools import partial
from pathlib import Path

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path

from ..const import DOMAIN
from ..history import dayfile, pace

EXPIRY_SECONDS = 600
URL = "/api/ld2410_tuner/export/{token}"
_TOKEN = re.compile(r"[0-9a-f]{32}")


def folder(hass):
    return Path(hass.config.path(".storage", DOMAIN, "exports"))


async def create(runtime, device_id, exported):
    """Write ``exported`` (the radar's metadata) with the radar's day files; its download."""
    hass = runtime.hass
    await discard(runtime)
    token = secrets.token_hex(16)
    path = folder(hass) / f"{token}.json"
    size = await hass.async_add_executor_job(_write, path, exported, runtime._days, device_id)
    filename = "ld2410-tuner-{}.json".format(re.sub(r"[^A-Za-z0-9_.-]", "_", device_id))
    timer = asyncio.get_running_loop().call_later(EXPIRY_SECONDS, _expire, runtime, token)
    runtime._export_file = {"token": token, "path": path, "filename": filename, "timer": timer}
    url = async_sign_path(hass, URL.format(token=token), timedelta(seconds=EXPIRY_SECONDS))
    return {"url": url, "filename": filename, "bytes": size}


@pace.background
def _write(path, exported, days, device_id):
    if device_id in exported["devices"]:
        exported["devices"][device_id]["history"]["day_files"] = day_files(days, device_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as out:
            json.dump(exported, out, indent=2, allow_nan=False)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path.stat().st_size


def day_files(days, device_id):
    """``{date: base64 day file}`` of the radar's recorded days, the day being recorded as it
    stands: the stored format, compressed and lossless (``history.dayfile.decode_day``)."""
    if days is None:
        return {}
    found = {}
    for date in days.days(device_id):
        pace.rest()
        raw = days.stored_file(device_id, date)
        if raw is None:
            day = days.day(device_id, date)
            if day is None:
                continue
            raw = dayfile.encode_day(day)
        found[date.isoformat()] = base64.b64encode(raw).decode("ascii")
    return found


def _expire(runtime, token):
    current = getattr(runtime, "_export_file", None)
    if current is not None and current["token"] == token:
        runtime._export_cleanup = runtime.hass.async_create_task(discard(runtime))


async def discard(runtime):
    """Remove the current export, if any (a new export, its link's expiry, unload)."""
    current = getattr(runtime, "_export_file", None)
    if current is None:
        return
    runtime._export_file = None
    current["timer"].cancel()
    await runtime.hass.async_add_executor_job(partial(current["path"].unlink, missing_ok=True))


async def clear(hass):
    """Remove exports left by a previous run (startup)."""
    await hass.async_add_executor_job(partial(shutil.rmtree, folder(hass), ignore_errors=True))


class ExportView(HomeAssistantView):
    """Serves the current export to an admin, as the signed link from ``create`` names it."""

    url = URL
    name = "api:ld2410_tuner:export"
    requires_auth = True

    def __init__(self, hass):
        self.hass = hass

    async def get(self, request, token):
        if not request["hass_user"].is_admin:
            raise web.HTTPForbidden
        current = getattr(self.hass.data.get(DOMAIN), "_export_file", None)
        if (
            current is None
            or not _TOKEN.fullmatch(token)
            or not secrets.compare_digest(token, current["token"])
        ):
            raise web.HTTPNotFound
        disposition = f'attachment; filename="{current["filename"]}"'
        return web.FileResponse(current["path"], headers={"Content-Disposition": disposition})

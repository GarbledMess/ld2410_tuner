"""The JSON export's file download through a real Home Assistant; synthetic data only.

Run first in a fresh disposable HA (it onboards): one synthetic radar is stored before the
integration is added, its export is requested over the websocket and downloaded from the
signed link with no other credentials. A tampered signature and an unknown export refuse.
"""

import asyncio
import json
import secrets
import sys
import time
import types
import urllib.parse
from pathlib import Path

import aiohttp

package = types.ModuleType("probe_tuner")
package.__path__ = [str(Path(__file__).resolve().parents[2] / "custom_components/ld2410_tuner")]
sys.modules[package.__name__] = package
from probe_tuner.const import (  # noqa: E402
    DOMAIN,
    STORAGE_KEY,
    STORAGE_MINOR_VERSION,
    STORAGE_VERSION,
)

BASE = "http://127.0.0.1:8123"
CONFIG = Path("/config")


async def onboard(session):
    for _ in range(180):
        try:
            async with session.get(BASE + "/api/onboarding") as response:
                if response.status == 200:
                    break
        except aiohttp.ClientError:
            pass
        await asyncio.sleep(1)
    client = BASE + "/"
    user = {
        "name": "Disposable lab",
        "username": "lab_" + secrets.token_hex(8),
        "password": secrets.token_urlsafe(32),
        "client_id": client,
        "language": "en",
    }
    async with session.post(BASE + "/api/onboarding/users", json=user) as response:
        code = (await response.json())["auth_code"]
    form = {"grant_type": "authorization_code", "code": code, "client_id": client}
    async with session.post(BASE + "/auth/token", data=form) as response:
        return (await response.json())["access_token"]


async def add_integration(session, headers):
    flow_path = BASE + "/api/config/config_entries/flow"
    async with session.post(flow_path, json={"handler": DOMAIN}, headers=headers) as response:
        flow = await response.json()
    while flow.get("type") == "form":
        url = flow_path + "/" + urllib.parse.quote(flow["flow_id"], safe="")
        async with session.post(url, json={}, headers=headers) as response:
            flow = await response.json()
    assert flow.get("type") == "create_entry", flow.get("type")


async def command(socket, ident, message, timeout=120):
    await socket.send_json({"id": ident, **message})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        reply = await socket.receive_json(timeout=timeout)
        if reply.get("id") == ident:
            return reply
    raise TimeoutError(message["type"])


async def export(session, token):
    async with session.ws_connect(BASE + "/api/websocket") as socket:
        await socket.receive_json()
        await socket.send_json({"type": "auth", "access_token": token})
        assert (await socket.receive_json())["type"] == "auth_ok"
        for ident in range(1, 120):  # The runtime starts in the background after setup.
            reply = await command(socket, ident, {"type": f"{DOMAIN}/snapshot"})
            if reply.get("success"):
                break
            await asyncio.sleep(1)
        else:
            raise TimeoutError("the integration's runtime did not start")
        reply = await command(socket, 200, {"type": f"{DOMAIN}/export_file", "device_id": "probe"})
        assert reply.get("success"), reply.get("error")
        return reply["result"]


async def main():
    store = {
        "version": STORAGE_VERSION,
        "minor_version": STORAGE_MINOR_VERSION,
        "key": STORAGE_KEY,
        "data": {"devices": {"probe": {"training_state": "unknown"}}},
    }
    async with aiohttp.ClientSession() as session:
        token = await onboard(session)
        headers = {"Authorization": "Bearer " + token}
        (CONFIG / ".storage").mkdir(exist_ok=True)
        (CONFIG / ".storage" / STORAGE_KEY).write_text(json.dumps(store), encoding="utf-8")
        await add_integration(session, headers)
        result = await export(session, token)
        evidence = {"filename": result["filename"], "bytes": result["bytes"]}
        async with session.get(BASE + result["url"]) as response:  # The signed link alone.
            body = await response.read()
            evidence["download_status"] = response.status
            evidence["disposition"] = response.headers.get("Content-Disposition")
        exported = json.loads(body)
        assert evidence["download_status"] == 200
        assert evidence["disposition"] == f'attachment; filename="{result["filename"]}"'
        assert exported["format"] == "ld2410_tuner_export_v2"
        assert "probe" in exported["devices"] and len(body) == result["bytes"]
        tampered = result["url"][:-4] + ("aaaa" if not result["url"].endswith("aaaa") else "bbbb")
        async with session.get(BASE + tampered) as response:
            evidence["tampered_status"] = response.status
        assert evidence["tampered_status"] in (401, 403)
        path = f"/api/{DOMAIN}/export/{secrets.token_hex(16)}"
        async with session.get(BASE + path, headers=headers) as response:
            evidence["unknown_status"] = response.status
        assert evidence["unknown_status"] == 404
        print(json.dumps(evidence), flush=True)


if __name__ == "__main__":
    asyncio.run(main())

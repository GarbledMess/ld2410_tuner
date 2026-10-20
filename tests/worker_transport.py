"""In-process process transport; real acquisition, protocol and numerical dispatch."""

import asyncio
from pathlib import Path

from tuner_under_test.worker import calculations, codec, protocol, transactions


class Transport:
    def __init__(self):
        self.before = None
        self.after = None
        self.respond = None
        self.requests = []

    async def process(self, args):
        if len(args) == 3 and args[2] == "storage":
            request = transactions._read(args[0], "learn")
            self.requests.append(request)
            if self.before is not None:
                await self.before(request)
            result = await asyncio.to_thread(transactions.execute, request)
            if self.after is not None:
                await self.after(request)
            return result
        request = protocol.unpack(Path(args[0]).read_bytes(), "learn")
        self.requests.append(request)
        if self.before is not None:
            await self.before(request)
        if self.respond is not None:
            result = await self.respond(request)
        else:
            result = await asyncio.to_thread(calculations.execute, request)
        if self.after is not None:
            await self.after(request)
        return codec.pack_result(result)


async def executor(fn, *args):
    """Keep acquisition on a real executor even in otherwise synchronous I/O tests."""
    return await asyncio.to_thread(fn, *args)

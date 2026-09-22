"""Local, authenticated audio relay between Asterisk AudioSocket and Chromium."""

import asyncio
import contextlib
from pathlib import Path
import secrets
import struct
import uuid

from aiohttp import web, WSMsgType

WEB = Path(__file__).resolve().parent.parent / "web"


def packet(payload):
    return struct.pack("!BH", 0x10, len(payload)) + payload


async def read_packet(reader):
    kind, size = struct.unpack("!BH", await reader.readexactly(3))
    if size > 3200:
        raise ValueError("Oversized AudioSocket frame")
    return kind, await reader.readexactly(size)


class MediaCall:
    def __init__(self, call_id):
        self.call_id = call_id
        self.token = secrets.token_hex(32)
        self.answered = asyncio.Event()
        self.done = asyncio.Event()
        self.socket = self.writer = None
        self.to_phone = asyncio.Queue(maxsize=25)
        self.rx_bytes = self.tx_bytes = 0

    async def close(self):
        self.done.set()
        if self.socket:
            await self.socket.close()
        if self.writer:
            self.writer.close()
            with contextlib.suppress(OSError):
                await self.writer.wait_closed()


class MediaServer:
    def __init__(self, config, health, control=None):
        self.config, self.health = config, health
        self.active = None
        self.runner = self.server = None
        self.control = control

    async def start(self):
        app = web.Application(client_max_size=8192)

        async def health(_):
            status = self.health()
            return web.json_response(status, status=200 if status["healthy"] else 503)

        async def asset(request):
            path = {"/": "relay.html", "/relay.js": "dist/relay.js", "/pcm-worklet.js": "pcm-worklet.js"}[request.path]
            return web.FileResponse(WEB / path, headers={"Cache-Control": "no-store"})

        app.router.add_get("/healthz", health)
        if self.control:
            app.router.add_post("/control", self.control)
        for path in ("/", "/relay.js", "/pcm-worklet.js"):
            app.router.add_get(path, asset)
        app.router.add_get("/media/{call_id}", self.websocket)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", self.config.http_port).start()
        self.server = await asyncio.start_server(self.audio, "127.0.0.1", self.config.audio_port)

    async def websocket(self, request):
        call = self.active
        tokens = [part.strip() for part in request.headers.get("Sec-WebSocket-Protocol", "").split(",")]
        origin = f"http://127.0.0.1:{self.config.http_port}"
        if (call is None or request.match_info["call_id"] != call.call_id or call.socket is not None
                or request.headers.get("Origin") != origin
                or not any(secrets.compare_digest(token, call.token) for token in tokens)):
            raise web.HTTPForbidden()
        socket = web.WebSocketResponse(protocols=["huddle-audio"], heartbeat=15, max_msg_size=3200)
        await socket.prepare(request)
        call.socket = socket
        try:
            async for message in socket:
                if message.type == WSMsgType.BINARY:
                    if len(message.data) != 320:
                        break
                    # Audio may arrive before the handset is answered. Drop it.
                    if not call.answered.is_set():
                        continue
                    if call.to_phone.full():
                        call.to_phone.get_nowait()
                    call.to_phone.put_nowait(message.data)
                elif message.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            call.done.set()
            await socket.close()
        return socket

    async def audio(self, reader, writer):
        call = None
        tasks = []
        try:
            kind, value = await asyncio.wait_for(read_packet(reader), 5)
            active = self.active
            if (kind != 1 or len(value) != 16 or active is None
                    or str(uuid.UUID(bytes=value)) != active.call_id or active.writer is not None
                    or active.done.is_set() or active.socket is None):
                return
            call = active
            call.writer = writer
            call.answered.set()

            async def receive():
                while not call.done.is_set():
                    kind, data = await read_packet(reader)
                    if kind in (0, 0xFF):
                        return
                    if kind == 0x10:
                        if not data or len(data) % 2:
                            raise ValueError("Invalid PCM frame")
                        await asyncio.wait_for(call.socket.send_bytes(data), 2)
                        call.rx_bytes += len(data)
                    elif kind != 3:  # Ignore DTMF; other sample rates are unsupported.
                        raise ValueError("Unexpected AudioSocket frame type")

            async def send():
                deadline = asyncio.get_running_loop().time()
                while not call.done.is_set():
                    try:
                        data = call.to_phone.get_nowait()
                    except asyncio.QueueEmpty:
                        data = bytes(320)
                    writer.write(packet(data))
                    await asyncio.wait_for(writer.drain(), 2)
                    call.tx_bytes += len(data)
                    deadline += 0.02
                    now = asyncio.get_running_loop().time()
                    deadline = max(deadline, now)
                    await asyncio.sleep(max(0, deadline - now))

            tasks = [asyncio.create_task(receive()), asyncio.create_task(send()),
                     asyncio.create_task(call.done.wait())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (ValueError, OSError, TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if call:
                call.done.set()
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def close(self):
        if self.active:
            await self.active.close()
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        if self.runner:
            await self.runner.cleanup()

import asyncio
import socket
from types import SimpleNamespace
import unittest
import uuid

import aiohttp

from huddle_phone.media import MediaCall, MediaServer, read_packet


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.config = SimpleNamespace(http_port=free_port(), audio_port=free_port())
        self.server = MediaServer(self.config, lambda: {"healthy": True})
        await self.server.start()
        self.call = MediaCall(str(uuid.uuid4()))
        self.server.active = self.call
        self.http = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.server.close()
        await self.http.close()

    async def connect_browser(self, token=None):
        return await self.http.ws_connect(
            f"http://127.0.0.1:{self.config.http_port}/media/{self.call.call_id}",
            protocols=["huddle-audio", token or self.call.token],
            origin=f"http://127.0.0.1:{self.config.http_port}",
        )

    async def test_bidirectional_pcm_and_phone_disconnect(self):
        ws = await self.connect_browser()
        reader, writer = await asyncio.open_connection("127.0.0.1", self.config.audio_port)
        try:
            writer.write(b"\x01\x00\x10" + uuid.UUID(self.call.call_id).bytes)
            # Fragment a packet to exercise TCP stream framing.
            writer.write(b"\x10\x01")
            await writer.drain()
            writer.write(b"\x40" + b"\x12\x34" * 160)
            await writer.drain()
            message = await asyncio.wait_for(ws.receive(), 2)
            self.assertEqual(message.data, b"\x12\x34" * 160)
            await ws.send_bytes(b"\x56\x78" * 160)
            for _ in range(10):
                kind, payload = await asyncio.wait_for(read_packet(reader), 2)
                if payload == b"\x56\x78" * 160:
                    break
            else:
                self.fail("Chime-to-phone audio was not delivered")
            self.assertEqual(kind, 0x10)
            writer.write(b"\x00\x00\x00")
            await writer.drain()
            await asyncio.wait_for(self.call.done.wait(), 2)
        finally:
            writer.close()
            await writer.wait_closed()
            await ws.close()

    async def test_wrong_socket_token_is_rejected(self):
        with self.assertRaises(aiohttp.WSServerHandshakeError) as error:
            await self.connect_browser(token="wrong-token")
        self.assertEqual(error.exception.status, 403)

    async def test_unknown_audiosocket_uuid_never_answers_call(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.config.audio_port)
        writer.write(b"\x01\x00\x10" + uuid.uuid4().bytes)
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
        self.assertFalse(self.call.answered.is_set())
        writer.close()
        await writer.wait_closed()

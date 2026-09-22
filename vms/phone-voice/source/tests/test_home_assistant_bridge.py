import asyncio
import contextlib
import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio import packet
from home_assistant import Config, ConfigurationError, HomeAssistantError, Reply
from home_assistant_bridge import HomeAssistantBridge, HomeAssistantCall


class FakeSpeech:
    greeting = bytes(320)

    def __init__(self):
        self.spoken = []

    def synthesize(self, text):
        self.spoken.append(text)
        return bytes(320)

    def transcribe(self, pcm):
        return "Turn on the test lamp"


class CallTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.speech = FakeSpeech()
        self.bridge = HomeAssistantBridge(self.speech)
        self.bridge.state_dir = Path(self.folder.name)
        self.server = await asyncio.start_server(self.bridge.accept, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        await self.wait_for(lambda: not self.bridge.active)
        self.folder.cleanup()

    async def wait_for(self, condition):
        async with asyncio.timeout(3):
            while not condition():
                await asyncio.sleep(0.005)

    async def connect(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(packet(0x01, uuid.uuid4().bytes))
        await writer.drain()
        return reader, writer

    async def test_missing_config_speaks_setup_and_closes_without_incoming_audio(self):
        with patch("home_assistant_bridge.Config.load", side_effect=ConfigurationError("Missing token")):
            reader, writer = await self.connect()
            try:
                data = await asyncio.wait_for(reader.read(), 3)
                self.assertTrue(data, "Setup message must be audible")
                self.assertIn("isn't connected yet", self.speech.spoken[0])
                await self.wait_for(lambda: not self.bridge.active)
                status = json.loads((self.bridge.state_dir / "status.json").read_text())
                self.assertEqual(status["event"], "disconnected")
            finally:
                writer.close()
                await writer.wait_closed()

    async def test_error_keeps_call_usable_and_new_call_has_fresh_conversation(self):
        calls = []

        def capture(*args):
            call = HomeAssistantCall(*args)
            calls.append(call)
            return call

        assistant = AsyncMock()
        assistant.process.side_effect = [
            HomeAssistantError("Connection failed. Check the device status."),
            Reply("The test lamp is on.", "first-conversation", "action_done"),
            Reply("The test lamp is on.", "first-conversation", "action_done"),
            Reply("The test lamp is on.", "second-conversation", "action_done"),
        ]
        config = Config("http://localhost:8123", "test-token")
        with patch("home_assistant_bridge.Config.load", return_value=config), \
                patch("home_assistant_bridge.HomeAssistant", return_value=assistant), \
                patch("home_assistant_bridge.HomeAssistantCall", side_effect=capture):
            for call_index in range(2):
                reader, writer = await self.connect()
                await self.wait_for(lambda: len(calls) == call_index + 1 and not calls[-1].busy)
                call = calls[-1]
                try:
                    if call_index == 0:
                        call.enqueue(b"command")
                        await self.wait_for(lambda: not call.busy)
                        self.assertIn("Connection failed", self.speech.spoken[-1])
                        self.assertEqual(call.turn_count, 0)
                    for turn in range(2 if call_index == 0 else 1):
                        call.enqueue(b"command")
                        await self.wait_for(lambda: call.turn_count == turn + 1 and not call.busy)
                finally:
                    writer.close()
                    await writer.wait_closed()
                    with contextlib.suppress(ConnectionError):
                        await asyncio.wait_for(reader.read(), 1)
                    await self.wait_for(lambda: not self.bridge.active)
        self.assertEqual([entry.args[1] for entry in assistant.process.call_args_list],
                         [None, None, "first-conversation", None])


if __name__ == "__main__":
    unittest.main()

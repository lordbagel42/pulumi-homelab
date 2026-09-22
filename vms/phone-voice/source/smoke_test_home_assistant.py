#!/usr/bin/env python3
"""Real phone audio -> local speech -> fake Assist API -> local speech -> audio."""

import asyncio
import contextlib
import json
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

from audio import Speech, packet, read_packet
from home_assistant import Config
from home_assistant_bridge import GREETING, HomeAssistantBridge


async def main():
    requests = []

    async def fake_assist(reader, writer):
        try:
            raw = (await reader.readuntil(b"\r\n\r\n")).decode()
            headers = dict(line.split(": ", 1) for line in raw.split("\r\n")[1:] if ": " in line)
            assert raw.startswith("POST /api/conversation/process HTTP/1.1\r\n")
            assert headers["Authorization"] == "Bearer smoke-test-token"
            request = json.loads(await reader.readexactly(int(headers["Content-Length"])))
            requests.append(request)
            state = "off" if "off" in request["text"].lower() else "on"
            body = json.dumps({"conversation_id": "test-conversation", "response": {
                "response_type": "action_done",
                "speech": {"plain": {"speech": f"The test lamp is {state}."}},
                "data": {"success": [{"id": "light.test", "type": "entity"}], "failed": []},
            }}).encode()
            writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    speech = await asyncio.to_thread(Speech, GREETING, "Home Assistant. Turn on the test lamp. Turn off the test lamp.")
    bridge = HomeAssistantBridge(speech)
    with tempfile.TemporaryDirectory(prefix="home-assistant-smoke-") as folder:
        bridge.state_dir = Path(folder)
        async with await asyncio.start_server(fake_assist, "127.0.0.1", 0) as api:
            config = Config(f"http://127.0.0.1:{api.sockets[0].getsockname()[1]}", "smoke-test-token")
            with patch("home_assistant_bridge.Config.load", return_value=config):
                async with await asyncio.start_server(bridge.accept, "127.0.0.1", 0) as server:
                    port = server.sockets[0].getsockname()[1]
                    reader, writer = await asyncio.open_connection("127.0.0.1", port)
                    call_id = uuid.uuid4()
                    writer.write(packet(0x01, call_id.bytes))
                    await writer.drain()
                    received = bytearray()

                    async def receive():
                        while True:
                            kind, pcm = await read_packet(reader)
                            if kind == 0x10:
                                received.extend(pcm)

                    async def wait_listening(turns):
                        async with asyncio.timeout(90):
                            while True:
                                status_path = bridge.state_dir / "status.json"
                                if status_path.exists():
                                    status = json.loads(status_path.read_text())
                                    if (status["call_id"] == str(call_id)
                                            and status["event"] == "listening" and status["turns"] == turns):
                                        return
                                await asyncio.sleep(0.05)

                    receive_task = asyncio.create_task(receive())
                    try:
                        await wait_listening(0)
                        assert len(received) > 16000, "Greeting missing"
                        print("PASS: Home Assistant greeting received over AudioSocket", flush=True)
                        busy_reader, busy_writer = await asyncio.open_connection("127.0.0.1", port)
                        assert await asyncio.wait_for(read_packet(busy_reader), 5) == (0x00, b"")
                        busy_writer.close()
                        await busy_writer.wait_closed()
                        print("PASS: second caller rejected while a call is active", flush=True)
                        for turn, state in enumerate(("on", "off"), start=1):
                            command = await asyncio.to_thread(speech.synthesize, f"Turn {state} the test lamp.")
                            await asyncio.sleep(0.5)
                            received.clear()
                            outgoing = command + bytes(16000)
                            deadline = asyncio.get_running_loop().time()
                            for offset in range(0, len(outgoing), 320):
                                writer.write(packet(0x10, outgoing[offset:offset + 320].ljust(320, b"\0")))
                                await writer.drain()
                                deadline += 0.02
                                await asyncio.sleep(max(0, deadline - asyncio.get_running_loop().time()))
                            await wait_listening(turn)
                            assert len(requests) == turn, "Command was lost or retried"
                            recognized = requests[-1]["text"].lower()
                            assert "lamp" in recognized and state in recognized, recognized
                            answer = await asyncio.to_thread(speech.transcribe, bytes(received))
                            assert "lamp" in answer.lower() and state in answer.lower(), answer
                            print(f"PASS: '{requests[-1]['text']}' -> fake Assist -> '{answer}'", flush=True)
                        assert "conversation_id" not in requests[0]
                        assert requests[1]["conversation_id"] == "test-conversation"
                        print("PASS: Home Assistant conversation retained across turns", flush=True)
                    finally:
                        writer.close()
                        await writer.wait_closed()
                        receive_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError, asyncio.IncompleteReadError):
                            await receive_task
                        async with asyncio.timeout(5):
                            while bridge.active:
                                await asyncio.sleep(0.01)
                    status = json.loads((bridge.state_dir / "status.json").read_text())
                    assert status["event"] == "disconnected"
                    assert not {"last_user", "last_reply"} & status.keys()
                    print("PASS: hangup cleans up call; transcripts are not saved", flush=True)


if __name__ == "__main__":
    asyncio.run(main())

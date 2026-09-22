#!/usr/bin/env python3
"""Exercise real speech recognition, Codex, and speech playback without ringing."""

import asyncio
import contextlib
import json
import time
import uuid

from bridge import ROOT, Speech, packet, read_packet
from session_store import call_uuid


async def main():
    speech = await asyncio.to_thread(Speech)
    question = await asyncio.to_thread(
        speech.synthesize,
        "Hello Codex. What is two plus two? Please answer in one short sentence.")
    reader, writer = await asyncio.open_connection("127.0.0.1", 9092)
    call_id = call_uuid()
    writer.write(packet(0x01, call_id.bytes))
    await writer.drain()
    received = bytearray()

    async def receive():
        while True:
            kind, pcm = await read_packet(reader)
            if kind == 0x10:
                received.extend(pcm)

    async def wait_status(turns):
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            path = ROOT / "state/status.json"
            if path.exists():
                status = json.loads(path.read_text())
                if (status.get("call_id") == str(call_id)
                        and status.get("event") == "listening"
                        and status.get("turns") == turns):
                    return status
            await asyncio.sleep(0.1)
        raise TimeoutError("No completed spoken turn from the bridge")

    task = asyncio.create_task(receive())
    try:
        await wait_status(0)
        await asyncio.sleep(0.5)
        assert len(received) > 16000, "Greeting was not transmitted"
        print(f"Greeting received: {len(received) / 16000:.1f}s", flush=True)
        received.clear()
        outgoing = question + bytes(16000)
        deadline = asyncio.get_running_loop().time()
        for offset in range(0, len(outgoing), 320):
            writer.write(packet(0x10, outgoing[offset:offset + 320].ljust(320, b"\0")))
            await writer.drain()
            deadline += 0.02
            await asyncio.sleep(max(0, deadline - asyncio.get_running_loop().time()))
        status = await wait_status(1)
        await asyncio.sleep(0.1)
        answer = await asyncio.to_thread(speech.transcribe, bytes(received))
        print("Recognized caller:", status.get("last_user"), flush=True)
        print("Codex response:", status.get("last_reply"), flush=True)
        print("Recognized returned audio:", answer, flush=True)
        assert "four" in answer.lower() or "4" in answer, answer
        assert "two" in status["last_user"].lower() or "2" in status["last_user"], status
        print("PASS: spoken question -> Codex -> spoken answer", flush=True)
    finally:
        writer.write(packet(0x00))
        with contextlib.suppress(ConnectionError):
            await writer.drain()
        writer.close()
        await writer.wait_closed()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, asyncio.IncompleteReadError):
            await task


if __name__ == "__main__":
    asyncio.run(main())

"""Manual integration checks against the running bridge; no handset ringing."""

import asyncio
import contextlib
import json
import re
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from audio import ROOT, Speech, packet, read_packet
from control import request
from session_store import call_uuid


async def until(check, timeout=90):
    async with asyncio.timeout(timeout):
        while True:
            result = check()
            if asyncio.iscoroutine(result):
                result = await result
            if result:
                return result
            await asyncio.sleep(0.05)


class Connection:
    async def open(self, number):
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", 9092)
        self.uuid = call_uuid(number)
        self.audio = bytearray()
        self.writer.write(packet(0x01, self.uuid.bytes))
        await self.writer.drain()
        self.receiver = asyncio.create_task(self.receive())
        await until(lambda: self.status().get("call_id") == str(self.uuid))
        return self

    def status(self):
        return json.loads((ROOT / "state/status.json").read_text())

    async def receive(self):
        while True:
            kind, payload = await read_packet(self.reader)
            if kind == 0x10:
                self.audio.extend(payload)

    async def key(self, digit):
        self.writer.write(packet(0x03, digit.encode()))
        await self.writer.drain()

    async def speak(self, pcm):
        # Real RTP keeps flowing during silence; emulate enough of it to pass
        # the bridge's 1.2-second endpoint after the final synthesized syllable.
        outgoing = pcm + bytes(25600)
        deadline = asyncio.get_running_loop().time()
        for offset in range(0, len(outgoing), 320):
            self.writer.write(packet(0x10, outgoing[offset:offset + 320].ljust(320, b"\0")))
            await self.writer.drain()
            deadline += 0.02
            await asyncio.sleep(max(0, deadline - asyncio.get_running_loop().time()))

    async def close(self):
        self.writer.write(packet(0x00))
        await self.writer.drain()
        self.writer.close()
        await self.writer.wait_closed()
        self.receiver.cancel()
        with contextlib.suppress(asyncio.CancelledError, asyncio.IncompleteReadError):
            await self.receiver
        await until(lambda: self.status().get("event") == "disconnected", timeout=5)


async def managed(speech):
    session = await request("create", title="Automated hangup and interruption verification")
    number = session["id"]
    call = await Connection().open(number)
    try:
        await until(lambda: len(call.audio) >= 6400)
        pcm = await asyncio.to_thread(speech.synthesize, "Stop talking.")
        before = len(call.audio)
        await call.speak(pcm)
        await until(lambda: "stop talking" in call.status().get("last_user", "").lower())
        assert len(call.audio) - before < 32000, "Playback did not stop promptly"
        print("PASS: spoken interruption stops playback before the greeting ends", flush=True)
        await call.key("0")
        await until(lambda: call.status().get("event") == "speaking")
        await asyncio.sleep(0.2)
        await call.key("*")
        await asyncio.sleep(0.2)
        after = len(call.audio)
        await asyncio.sleep(0.3)
        assert len(call.audio) == after, "Keypad interruption did not stop playback"
        print("PASS: star stops playback immediately", flush=True)
        await request("submit", session_id=number, text=(
            "This is an automated persistence test. Run the shell command sleep 12. "
            "After it finishes, reply exactly: The session survived hangup. Do not use other tools or ask questions."))
        async def started():
            status = await request("session", session_id=number)
            return status if status["state"] == "running" and "command" in status["progress"].lower() else None
        active = await until(started)
        thread = active["thread_id"]
    finally:
        await call.close()
    await asyncio.sleep(1)
    assert (await request("session", session_id=number))["state"] == "running"
    async def done():
        result = await request("session", session_id=number)
        if result["state"] == "error":
            raise AssertionError(result["error"])
        return result if result["state"] == "done" else None
    result = await until(done)
    assert "survived hangup" in result["last_reply"].lower(), result
    assert result["thread_id"] == thread
    print("PASS: real Codex shell work finishes after telephone hangup", flush=True)
    call = await Connection().open(number)
    try:
        await until(lambda: call.status().get("event") == "listening")
        words = await asyncio.to_thread(speech.transcribe, bytes(call.audio))
        assert "survived" in words.lower(), words
        print("PASS: callback announces the saved result from the same session", flush=True)
        await request("submit", session_id=number, text=(
            "This is an automated explicit interruption test. Run the shell command sleep 60. "
            "If it completes, say the timer completed. Do not use other tools or ask questions."))
        await until(started)
        await call.key("*")
        await asyncio.sleep(0.15)
        await call.key("*")
        async def stopped():
            return (await request("session", session_id=number))["state"] == "interrupted"
        await until(stopped, timeout=15)
        assert (await request("session", session_id=number))["thread_id"] == thread
        print("PASS: double star interrupts the actual Codex turn, preserving history", flush=True)
    finally:
        await call.close()


def structured(result):
    if result.isError:
        raise AssertionError(result.content)
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(result.content[0].text)


async def mcp_roundtrip():
    parameters = StdioServerParameters(command=sys.executable, args=[str(ROOT / "phone_mcp.py")])
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            tools = await client.list_tools()
            assert {"ask_user", "get_answer", "update_session", "check_messages"} <= {t.name for t in tools.tools}
            session = structured(await client.call_tool("update_session", {
                "summary": "Automated MCP transport verification", "title": "MCP integration test", "state": "running"}))
            number = session["id"]
            call = await Connection().open(number)
            try:
                await call.key("*")
                result = structured(await client.call_tool("ask_user", {
                    "question": "For this automated transport test, choose blue or green.",
                    "options": ["Blue", "Green"], "session_id": str(number), "wait_seconds": 0}))
                question_id = result["question_id"]
                assert result["state"] == "pending" and not result["answers"]
                await until(lambda: "choose blue" in call.status().get("spoken_text", "").lower())
                await call.key("2")
                answer = structured(await client.call_tool("get_answer", {
                    "question_id": question_id, "wait_seconds": 10}))
                assert answer["answers"] == {"answer": {"answers": ["Green"]}}, answer
                assert answer["state"] == "answered"
                await client.call_tool("update_session", {"session_id": str(number), "state": "done",
                                                         "summary": "MCP phone question and answer passed."})
                print("PASS: real stdio MCP -> phone prompt -> keypad answer -> MCP response, without a confirmation menu",
                      flush=True)
            finally:
                await call.close()


async def main():
    async def ready():
        try:
            await request("sessions")
            return True
        except OSError:
            return False
    await until(ready, timeout=15)
    speech = await asyncio.to_thread(Speech)
    await managed(speech)
    await mcp_roundtrip()


if __name__ == "__main__":
    asyncio.run(main())

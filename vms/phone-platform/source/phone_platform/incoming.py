"""Bounded, loopback-only FastAGI gate. Never answers or originates a channel."""

import asyncio
import contextlib
import re


class IncomingGate:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.channel = self.uniqueid = None
        self.task = None
        self.responses = asyncio.Queue(maxsize=1)
        self.closed = False

    async def read_environment(self, handset):
        if self.writer.get_extra_info("peername")[0] != "127.0.0.1":
            raise ValueError("FastAGI requires loopback")
        environment = {}
        async with asyncio.timeout(3):
            for _ in range(40):
                line = await self.reader.readline()
                if line in (b"\n", b"\r\n"):
                    break
                if not line or len(line) > 1024:
                    raise ValueError("Invalid FastAGI environment")
                key, separator, value = line.decode("ascii").strip().partition(":")
                if not separator or key in environment:
                    raise ValueError("Invalid FastAGI field")
                environment[key] = value.strip()
            else:
                raise ValueError("Oversized FastAGI environment")
        self.channel = environment.get("agi_channel", "")
        self.uniqueid = environment.get("agi_uniqueid", "")
        if (
            not handset
            or not re.fullmatch(re.escape(handset) + r"-[0-9a-fA-F]+", self.channel)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", self.uniqueid)
            or environment.get("agi_network") != "yes"
            or environment.get("agi_network_script") != "june"
            or environment.get("agi_context") != "switchboard-incoming"
            or environment.get("agi_extension") != "s"
        ):
            raise ValueError("Unexpected inbound channel or route")

    async def watch(self, call):
        try:
            while True:
                line = await self.reader.readline()
                if not line or line.strip() == b"HANGUP":
                    call.end("phone_hangup")
                    return
                if line.strip() != b"200 result=1" or self.responses.full():
                    raise ValueError("FastAGI command failed")
                self.responses.put_nowait(None)
        except (OSError, ValueError):
            call.end("phone_control_error")

    async def release(self, media_id, port, max_seconds):
        # Asterisk resumes its dialplan on EOF. Commit the acceptance marker
        # last, only after the other variables were acknowledged successfully.
        for name, value in (
            ("SWITCHBOARD_CALL_ID", media_id),
            ("SWITCHBOARD_AUDIO_PORT", port),
            ("SWITCHBOARD_MAX_SECONDS", max_seconds),
            ("SWITCHBOARD_ACCEPTED", 1),
        ):
            self.writer.write(f'SET VARIABLE {name} "{value}"\n'.encode())
            await self.writer.drain()
            await self.responses.get()
        await self.close()

    async def close(self):
        if self.closed:
            return
        self.closed = True
        if self.task:
            self.task.cancel()
        self.writer.close()
        if self.task:
            await asyncio.gather(self.task, return_exceptions=True)
        with contextlib.suppress(OSError, TimeoutError):
            await asyncio.wait_for(self.writer.wait_closed(), 2)

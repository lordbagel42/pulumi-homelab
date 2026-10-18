"""One AMI connection per stream, with exact-channel cancellation.

Unlike the huddle service's multi-call AMI client, this adapter retains a single
origination until its ring deadline even if HTTP/WS delivery has been cancelled.
It never retries Originate and never hangs up by destination or channel prefix.
"""

import asyncio
import contextlib


class PhoneBusy(Exception):
    pass


class ChannelGone(ConnectionError):
    pass


class PhonePBX:
    def __init__(self, config, call_id):
        self.config, self.call_id = config, call_id
        self.writer = self.reader = self.task = None
        self.pending = {}
        self.serial = 0
        self.channel = None
        self.channel_known = asyncio.Event()
        self.ended = asyncio.Event()
        self.failure = asyncio.Event()
        self.attempted = False
        self.ring_deadline = 0

    def adopt(self, channel):
        """Track an exact channel already validated by the local FastAGI gate."""
        self.channel = channel
        self.channel_known.set()
        self.attempted = True

    async def connect(self):
        try:
            async with asyncio.timeout(5):
                self.reader, self.writer = await asyncio.open_connection(self.config.ami_host, self.config.ami_port)
                if not (await self.reader.readline()).startswith(b"Asterisk Call Manager/"):
                    raise ConnectionError("Unexpected AMI banner")
                self.task = asyncio.create_task(self.read())
                await self.action(
                    "Login", [("Username", self.config.ami_user), ("Secret", self.config.ami_secret), ("Events", "on")]
                )
        except BaseException:
            await self.close()
            raise

    async def read(self):
        try:
            while True:
                raw = await self.reader.readuntil(b"\r\n\r\n")
                message = {}
                for line in raw.decode(errors="replace").split("\r\n"):
                    if ":" in line:
                        key, value = line.split(":", 1)
                        message[key.lower()] = value.strip()
                action_id = message.get("actionid")
                if "event" not in message:
                    future = self.pending.get(action_id)
                    if future is not None and not future.done():
                        future.set_result(message)
                    continue
                if (
                    message.get("uniqueid") == self.call_id
                    and message["event"] == "Newchannel"
                    and message.get("channel")
                ):
                    self.channel = message["channel"]
                    self.channel_known.set()
                if message["event"] == "Hangup" and (
                    message.get("uniqueid") == self.call_id or (self.channel and message.get("channel") == self.channel)
                ):
                    self.ended.set()
                if message["event"] == "OriginateResponse" and action_id == self.call_id:
                    if message.get("response") == "Failure":
                        self.ended.set()
                    elif message.get("channel"):
                        self.channel = message["channel"]
                        self.channel_known.set()
        except (OSError, ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            self.failure.set()
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("AMI connection lost"))

    async def action(self, name, fields, *, action_id=None):
        self.serial += 1
        action_id = action_id or f"{self.call_id}-{self.serial}"
        pairs = [("Action", name), ("ActionID", action_id), *fields]
        if any("\r" in str(value) or "\n" in str(value) for pair in pairs for value in pair):
            raise ValueError("Invalid AMI header")
        wire = ("".join(f"{key}: {value}\r\n" for key, value in pairs) + "\r\n").encode()
        future = asyncio.get_running_loop().create_future()
        self.pending[action_id] = future
        try:
            async with asyncio.timeout(5):
                self.writer.write(wire)
                await self.writer.drain()
                result = await future
            if result.get("response") != "Success":
                if (
                    name == "Hangup"
                    and result.get("response") == "Error"
                    and result.get("message") == "No such channel"
                ):
                    raise ChannelGone("AMI channel no longer exists")
                # Server messages may contain supplied credentials. Never echo them.
                raise ConnectionError("AMI action rejected")
            return result
        finally:
            self.pending.pop(action_id, None)

    async def originate(self, audio_port, max_seconds):
        state = await self.action("Getvar", [("Variable", f"DEVICE_STATE({self.config.channel})")])
        if state.get("value") != "NOT_INUSE":
            raise PhoneBusy()
        self.attempted = True
        self.ring_deadline = asyncio.get_running_loop().time() + self.config.ring_seconds + 2
        await self.action(
            "Originate",
            [
                ("Channel", self.config.channel),
                ("ChannelId", self.call_id),
                ("Context", "switchboard-stream"),
                ("Exten", "s"),
                ("Priority", 1),
                ("Async", "true"),
                ("Timeout", self.config.ring_seconds * 1000),
                ("CallerID", '"Switchboard" <612>'),
                ("Variable", 'CONNECTEDLINE(all)="Switchboard" <612>'),
                ("Variable", "CONNECTEDLINE(name-pres,i)=allowed"),
                ("Variable", "CONNECTEDLINE(num-pres,i)=allowed"),
                ("Variable", f"SWITCHBOARD_CALL_ID={self.call_id}"),
                ("Variable", f"SWITCHBOARD_AUDIO_PORT={audio_port}"),
                ("Variable", f"SWITCHBOARD_MAX_SECONDS={max_seconds}"),
            ],
            action_id=self.call_id,
        )

    async def hangup(self):
        """Return 'ended' if already gone, 'requested' if accepted, or None if uncertain."""
        if not self.attempted or self.ended.is_set():
            return "ended"
        if not self.channel and not self.failure.is_set():
            waits = [asyncio.create_task(event.wait()) for event in (self.channel_known, self.ended, self.failure)]
            try:
                await asyncio.wait(
                    waits,
                    timeout=max(0, self.ring_deadline - asyncio.get_running_loop().time()),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for task in waits:
                    task.cancel()
                await asyncio.gather(*waits, return_exceptions=True)
        if self.ended.is_set():
            return "ended"
        if self.channel and self.writer and not self.writer.is_closing() and not self.failure.is_set():
            try:
                await self.action("Hangup", [("Channel", self.channel)])
                return "requested"
            except ChannelGone:
                # A handset hangup can beat both its AMI event and our action.
                # This exact-channel response confirms it is already gone.
                self.ended.set()
                return "ended"
            except (OSError, TimeoutError, ConnectionError):
                pass
        return None

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.writer:
            self.writer.close()
            with contextlib.suppress(OSError, TimeoutError):
                await asyncio.wait_for(self.writer.wait_closed(), 2)

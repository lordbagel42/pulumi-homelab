"""Small AMI client with correlated responses and per-call channel tracking."""

import asyncio
import contextlib
import uuid


def phone_name(value):
    # SCCP's name field holds 40 bytes including its terminator. Do not let a
    # Slack profile inject AMI headers or caller-ID delimiters. Keep UTF-8 whole.
    value = value if isinstance(value, str) else ""
    value = " ".join("".join(c if c.isprintable() and c not in '\\"<>' else " " for c in value).split())
    return value.encode("utf-8")[:39].decode("utf-8", errors="ignore").strip() or "Slack huddle"


def encode(fields):
    result = []
    for key, value in fields:
        value = str(value)
        if any(c in key + value for c in "\r\n"):
            raise ValueError("AMI header contains a newline")
        result.append(f"{key}: {value}\r\n")
    return ("".join(result) + "\r\n").encode()


class AMI:
    def __init__(self, config):
        self.config = config
        self.pending = {}
        self.channels = {}
        self.channel_ready = {}
        self.calls = {}
        self.reader = self.writer = self.task = None
        self.failure = asyncio.Event()

    async def connect(self):
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(self.config.ami_host, self.config.ami_port), 5)
        banner = await asyncio.wait_for(self.reader.readline(), 5)
        if not banner.startswith(b"Asterisk Call Manager/"):
            raise RuntimeError("Unexpected AMI banner")
        self.task = asyncio.create_task(self._read())
        try:
            await self.action([("Action", "Login"), ("Username", self.config.ami_user),
                               ("Secret", self.config.ami_secret), ("Events", "on")])
        except BaseException:
            await self.close()
            raise

    async def _read(self):
        try:
            while True:
                raw = await self.reader.readuntil(b"\r\n\r\n")
                message = {}
                for line in raw.decode(errors="replace").split("\r\n"):
                    if ":" in line:
                        key, value = line.split(":", 1)
                        message[key.lower()] = value.strip()
                action_id = message.get("actionid")
                # OriginateResponse has a Response header but is an EVENT.
                if "event" not in message and action_id in self.pending:
                    future = self.pending[action_id]
                    if not future.done():
                        future.set_result(message)
                call_id = message.get("uniqueid")
                if call_id in self.calls:
                    if message.get("event") == "Newchannel":
                        self.channels[call_id] = message["channel"]
                        self.channel_ready[call_id].set()
                    if message.get("event") == "Hangup":
                        self.calls[call_id].set()
                if message.get("event") == "OriginateResponse" and action_id in self.calls:
                    if message.get("response") == "Failure":
                        self.calls[action_id].set()
                    elif message.get("channel"):
                        self.channels[action_id] = message["channel"]
                        self.channel_ready[action_id].set()
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            self.failure.set()
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("AMI connection lost"))
            for done in self.calls.values():
                done.set()

    async def action(self, fields, *, action_id=None):
        action_id = action_id or str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self.pending[action_id] = future
        try:
            self.writer.write(encode([("ActionID", action_id), *fields]))
            await self.writer.drain()
            result = await asyncio.wait_for(future, 5)
            if result.get("response") not in ("Success", "Follows"):
                # Do not log server responses: they can contain supplied secrets.
                raise RuntimeError(f"AMI {fields[0][1]} rejected")
            return result
        finally:
            self.pending.pop(action_id, None)

    async def originate(self, call_id, *, caller_name="Slack huddle"):
        identity = f'"{phone_name(caller_name)}" <612>'
        self.calls[call_id] = asyncio.Event()
        self.channel_ready[call_id] = asyncio.Event()
        await self.action([
            ("Action", "Originate"), ("Channel", self.config.phone_channel),
            ("ChannelId", call_id), ("Context", "huddle-phone"),
            ("Exten", "s"), ("Priority", 1), ("Async", "true"),
            ("Timeout", self.config.ring_seconds * 1000),
            ("CallerID", identity),
            # chan-sccp uses connected-party updates for its on-screen call
            # information; AMI CallerID alone leaves the ringing name unknown.
            ("Variable", "CONNECTEDLINE(name-pres,i)=allowed"),
            ("Variable", "CONNECTEDLINE(num-pres,i)=allowed"),
            ("Variable", f"CONNECTEDLINE(all)={identity}"),
            ("Variable", f"HUDDLE_CALL_ID={call_id}"),
            ("Variable", f"HUDDLE_AUDIO_PORT={self.config.audio_port}"),
            ("Variable", f"HUDDLE_MAX_SECONDS={self.config.max_call_seconds}"),
        ], action_id=call_id)

    async def hangup(self, call_id):
        # Async Originate may acknowledge before Newchannel. If the huddle ends
        # during that gap, give the reader time to learn this call's exact name.
        if (call_id in self.calls and not self.calls[call_id].is_set()
                and call_id not in self.channels and not self.failure.is_set()):
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.channel_ready[call_id].wait(), 2)
        channel = self.channels.get(call_id)
        if channel and not self.failure.is_set():
            with contextlib.suppress(RuntimeError, ConnectionError, TimeoutError, OSError):
                await self.action([("Action", "Hangup"), ("Channel", channel)])
        self.channels.pop(call_id, None)
        self.calls.pop(call_id, None)
        self.channel_ready.pop(call_id, None)

    async def close(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        if self.writer:
            self.writer.close()
            with contextlib.suppress(OSError):
                await self.writer.wait_closed()

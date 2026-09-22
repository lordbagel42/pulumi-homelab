"""Opt-in Asterisk AudioSocket adapter for the Speech Engine audio plane.

No local STT, TTS or turn detector runs on this path. The listener is disabled
until a deployment explicitly assigns a port. PCM is held only in memory.
"""
import asyncio
import audioop
import base64
import contextlib
import json
import logging
import secrets
import re
import struct
import time
import uuid

from websockets.asyncio.client import connect

from .speech import SpeechError
from .speech_engine import SessionReply, pending_question, question_text
from .operator_profiles import profile_for, operator_instructions

LOG = logging.getLogger("switchboard.speech_engine")
FRAME_BYTES = 320
PCM_RATES = {0x10: 8000, 0x11: 12000, 0x12: 16000, 0x13: 24000,
             0x14: 32000, 0x15: 44100, 0x16: 48000, 0x17: 96000, 0x18: 192000}


def packet(kind, payload=b""):
    return struct.pack("!BH", kind, len(payload)) + payload


def call_route(store, payload):
    code = payload.hex()
    if code.startswith(("f100", "d100")):
        return "slack-operator"
    if code[:3].isdecimal() and int(code[:3]):
        meta = store.get("session-meta", "codex:" + str(int(code[:3])), {})
        if meta.get("integration") == "slack-huddles":
            return "slack-operator"
    return "codex"


async def read_packet(reader):
    kind, length = struct.unpack("!BH", await reader.readexactly(3))
    return kind, await reader.readexactly(length)


async def relay(reader, writer, host, port, handshake):
    """Transfer the existing handset to a local audio service, without dialing."""
    remote_reader, remote_writer = await asyncio.wait_for(asyncio.open_connection(host, port), 5)
    tasks = []
    async def pump(source, destination):
        while data := await source.read(65536):
            destination.write(data)
            await destination.drain()
    try:
        remote_writer.write(handshake)
        await remote_writer.drain()
        tasks = [asyncio.create_task(pump(reader, remote_writer)), asyncio.create_task(pump(remote_reader, writer))]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            await task
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        remote_writer.close()
        with contextlib.suppress(OSError):
            await remote_writer.wait_closed()


class EngineCall:
    def __init__(self, engine, reader, writer, connect_audio=connect):
        self.engine, self.reader, self.writer = engine, reader, writer
        self.connect_audio = connect_audio
        self.session_id = None
        self.conversation_id = None
        self.ready = asyncio.Event()
        self.audio = asyncio.Queue(maxsize=1500)  # At most 30 seconds of phone audio.
        self.output_remainder = b""
        self.output_state = None
        self.input_state = None
        self.input_rate = None
        self.generation = 0
        self.interrupted_event = -1
        self.muted = False
        self.last_star = 0.0
        self.send_lock = asyncio.Lock()
        self.bootstrap_token = "switchboard-connect-" + secrets.token_hex(24)
        self.greeting = ""
        self.route_id = "codex"
        self.instructions = ""
        self.call_id = None
        self.handoff_request = None
        self.profile_route = None
        self.reply = None

    def interrupt_audio(self, event_id=None):
        self.generation += 1
        if event_id is not None:
            self.interrupted_event = max(self.interrupted_event, event_id)
        self.output_state = None
        self.output_remainder = b""
        while not self.audio.empty():
            self.audio.get_nowait()

    async def send(self, ws, message):
        async with self.send_lock:
            await ws.send(json.dumps(message))

    async def phone_input(self, ws):
        while True:
            kind, payload = await read_packet(self.reader)
            if kind == 0x00:
                return
            if kind == 0xff:
                raise SpeechError("Asterisk audio connection failed")
            if kind == 0x03:
                key = payload.decode("ascii")
                if key == "*":
                    self.interrupt_audio()
                    self.muted = True
                    if time.monotonic() - self.last_star < 1.5:
                        await self.engine.connector.cancel(self.session_id)
                    self.last_star = time.monotonic()
                elif key == "#":
                    # The cloud owns turn detection; do not disconnect or submit
                    # a fabricated transcript when the old commit key is pressed.
                    continue
                elif key == "0":
                    self.interrupt_audio()
                    self.muted = False
                    await self.send(ws, {"type": "user_message", "text": "repeat that"})
                elif key in "123456789" and len(key) == 1:
                    state = await self.engine.connector.get_session(self.session_id)
                    if pending_question(state) or state.get("host") == "choose":
                        self.muted = False
                        await self.send(ws, {"type": "user_message", "text": "option " + key})
                continue
            if kind not in PCM_RATES:
                continue
            if len(payload) % 2:
                raise SpeechError("Invalid phone audio frame")
            rate = PCM_RATES[kind]
            if rate != self.input_rate:
                self.input_state = None
                self.input_rate = rate
            pcm, self.input_state = audioop.ratecv(payload, 2, 1, rate, 16000, self.input_state)
            await self.send(ws, {"user_audio_chunk": base64.b64encode(pcm).decode()})

    async def provider_input(self, ws):
        async for wire in ws:
            message = json.loads(wire)
            kind = message.get("type")
            if kind == "conversation_initiation_metadata":
                if self.conversation_id:
                    raise SpeechError("Duplicate audio initialization")
                meta = message["conversation_initiation_metadata_event"]
                if meta.get("user_input_audio_format") != "pcm_16000" or meta.get("agent_output_audio_format") != "pcm_16000":
                    raise SpeechError("Provision Speech Engine with 16 kHz PCM input and output")
                self.conversation_id = meta["conversation_id"]
                self.reply = SessionReply(self.engine.connector, self.session_id, bootstrap_token=self.bootstrap_token, greeting=self.greeting, instructions=self.instructions)
                self.engine.bind(self.conversation_id, self.reply)
                self.ready.set()
                await self.send(ws, {"type": "user_message", "text": self.bootstrap_token})
            elif kind == "ping":
                await self.send(ws, {"type": "pong", "event_id": message["ping_event"]["event_id"]})
            elif kind == "interruption":
                self.interrupt_audio(message["interruption_event"]["event_id"])
                self.muted = False
            elif kind == "user_transcript":
                self.muted = False
                text = message.get("user_transcription_event", {}).get("user_transcript", "")
                normalized = " ".join(re.sub(r"[^a-z0-9 ]", "", text.lower()).split())
                if normalized in {"hang up", "end the call", "end call", "goodbye"}:
                    return
            elif kind == "audio":
                if not self.ready.is_set():
                    raise SpeechError("Audio arrived before format negotiation")
                event = message["audio_event"]
                if self.muted or event["event_id"] <= self.interrupted_event:
                    continue
                data = base64.b64decode(event["audio_base_64"], validate=True)
                if len(data) % 2:
                    raise SpeechError("Invalid provider PCM")
                pcm, self.output_state = audioop.ratecv(data, 2, 1, 16000, 8000, self.output_state)
                pcm = self.output_remainder + pcm
                end = len(pcm) // FRAME_BYTES * FRAME_BYTES
                for offset in range(0, end, FRAME_BYTES):
                    # Do not block reception of interruption events behind audio.
                    try:
                        self.audio.put_nowait((self.generation, pcm[offset:offset+FRAME_BYTES]))
                    except asyncio.QueueFull:
                        raise SpeechError("Speech Engine audio exceeded the playback buffer") from None
                self.output_remainder = pcm[end:]
            elif kind == "agent_response_complete" and self.output_remainder:
                self.audio.put_nowait((self.generation, self.output_remainder.ljust(FRAME_BYTES, b"\0")))
                self.output_remainder = b""
            elif kind == "error":
                raise SpeechError("Speech Engine audio connection failed")

    async def player(self):
        deadline = asyncio.get_running_loop().time()
        while True:
            generation, frame = await self.audio.get()
            now = asyncio.get_running_loop().time()
            await asyncio.sleep(max(0, deadline-now))
            if generation != self.generation:
                continue
            self.writer.write(packet(0x10, frame))
            await self.writer.drain()
            deadline = max(deadline, asyncio.get_running_loop().time()) + .02

    async def watch_handoff(self):
        while True:
            handoff = self.engine.store.get("operator-handoffs", self.call_id, {})
            if handoff.get("request_id"):
                self.handoff_request = handoff["request_id"]
                return
            await asyncio.sleep(.2)

    async def join_huddle(self):
        client = self.engine.huddle_factory()
        async with asyncio.timeout(30):
            while True:
                status = await client.command("status", request_id=self.handoff_request)
                if status["phase"] == "awaiting-handset":
                    break
                if status["phase"] != "preparing":
                    raise SpeechError("Huddle preparation failed")
                await asyncio.sleep(.25)
        await relay(self.reader, self.writer, "127.0.0.1", self.engine.huddle_audio_port,
                    packet(1, uuid.UUID(status["call_id"]).bytes))

    async def close_slack(self):
        if self.route_id != "slack-operator" or not self.call_id:
            return
        call = self.engine.store.get("operator-calls", self.call_id, {})
        self.engine.store.put("operator-calls", self.call_id, {**call, "state": "closed"})
        # Persist closure first: a slow dial result must see the tombstone.
        if self.reply and self.reply.submissions:
            await asyncio.gather(*self.reply.submissions, return_exceptions=True)
        if self.session_id:
            with contextlib.suppress(Exception):
                await self.engine.connector.cancel(self.session_id)
        handoff = self.engine.store.get("operator-handoffs", self.call_id, {})
        if handoff.get("request_id"):
            with contextlib.suppress(Exception):
                await self.engine.huddle_factory().command("cancel", request_id=handoff["request_id"])

    async def run(self, handshake=None):
        try:
            await self._run(handshake)
        finally:
            await self.close_slack()

    async def _run(self, handshake=None):
        kind, payload = handshake or await asyncio.wait_for(read_packet(self.reader), 5)
        if kind != 1 or len(payload) != 16:
            raise SpeechError("Expected AudioSocket UUID")
        call_uuid = uuid.UUID(bytes=payload)
        self.route_id = call_route(self.engine.store, payload)
        prefix = call_uuid.hex[:3]
        if self.route_id == "codex" and not prefix.isdecimal():
            raise SpeechError("AudioSocket UUID must start with the three-digit session number")
        route = self.engine.store.get("routes", self.route_id, {})
        self.profile_route = self.route_id if "speech_engine" in route else None
        self.engine.settings(route=self.profile_route)
        integration = self.engine.store.get("integrations", route.get("integration", "codex"), {})
        if not route.get("enabled") or not integration.get("enabled"):
            raise SpeechError("The operator phone route is disabled")
        defaults = self.engine.store.get("settings", "main")
        agent_engine = defaults.get("default_engine", "amp")
        if self.route_id == "slack-operator":
            if self.engine.store.get("operator-calls", str(call_uuid)):
                raise SpeechError("This operator call has already been attempted")
            self.call_id = str(call_uuid)
            self.engine.store.put("operator-calls", self.call_id, {"call_id": self.call_id, "state": "active", "session_id": None, "created": time.time()})
            if call_uuid.hex.startswith("f100"):
                session = await self.engine.connector.request("create", title="Slack phone operator", host="proxmox", engine=agent_engine)
            else:
                session = await self.engine.connector.get_session(int(prefix))
            self.engine.store.put("session-meta", "codex:" + str(session["id"]), {"integration": "slack-huddles"})
            call = self.engine.store.get("operator-calls", self.call_id)
            self.engine.store.put("operator-calls", self.call_id, {**call, "session_id": "codex:" + str(session["id"])})
        else:
            number = int(prefix)
            if not number:
                session = await self.engine.connector.request("create", title="Phone task",
                    host=defaults.get("default_host", "proxmox"), engine=agent_engine)
            else:
                session = await self.engine.connector.get_session(number)
        self.session_id = session["id"]
        session = await self.engine.connector.get_session(self.session_id)
        profile = profile_for(self.engine.store, self.profile_route or self.route_id)
        self.instructions = operator_instructions(self.engine.store, self.profile_route or self.route_id,
                                                  self.call_id, owner_id=getattr(self.engine, "huddle_owner_id", ""))
        pending = pending_question(session)
        agent_name = "Amp" if session.get("engine", agent_engine) == "amp" else "Codex"
        greeting = profile["greeting"] or ("Slack operator. Who would you like to huddle with?" if self.route_id == "slack-operator" else f"This is {agent_name}.")
        if self.route_id == "codex":
            greeting += " Call back on " + " ".join(session["extension"]) + ". "
            greeting += question_text(pending[1]) if pending else session.get("last_reply") or session.get("progress") or "Tell me your task."
        elif pending:
            greeting += " " + question_text(pending[1])
        if session.get("host") == "choose":
            greeting += " Choose one for Proxmox or two for the workstation."
        self.greeting = greeting[:3000]
        url = await self.engine.signed_url(self.profile_route)
        tasks = []
        try:
            async with self.connect_audio(url, open_timeout=15, close_timeout=3,
                                          max_size=1024*1024, max_queue=16, proxy=None) as ws:
                await self.send(ws, {"type": "conversation_initiation_client_data",
                                    "conversation_config_override": {"agent": {"first_message": ""}}})
                receiver = asyncio.create_task(self.provider_input(ws))
                ready = asyncio.create_task(self.ready.wait())
                tasks.extend([receiver, ready])
                done, _ = await asyncio.wait(tasks, timeout=20, return_when=asyncio.FIRST_COMPLETED)
                if receiver in done:
                    await receiver
                    raise SpeechError("Speech Engine disconnected before initialization")
                if ready not in done:
                    raise SpeechError("Speech Engine initialization timed out")
                tasks.extend([asyncio.create_task(self.phone_input(ws)), asyncio.create_task(self.player())])
                if self.call_id:
                    tasks.append(asyncio.create_task(self.watch_handoff()))
                done, _ = await asyncio.wait([receiver, *tasks[2:]], return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    await task
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.conversation_id:
                await self.engine.unbind(self.conversation_id)
            # Never interrupt the durable agent task on hangup or media failure.
        if self.handoff_request:
            self.interrupt_audio()
            await self.join_huddle()


class AudioSocketServer:
    def __init__(self, engine, host, port, allowed_peers):
        self.engine, self.host, self.port = engine, host, port
        self.allowed_peers = set(allowed_peers)
        self.tasks = set()
        self.server = None
        self.busy = False

    async def accept(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        accepted = False
        try:
            peer = writer.get_extra_info("peername")
            if not peer or peer[0] not in self.allowed_peers or self.busy:
                return
            self.busy = True
            accepted = True
            handshake = await asyncio.wait_for(read_packet(reader), 5)
            kind, payload = handshake
            if kind != 1 or len(payload) != 16:
                raise SpeechError("Expected AudioSocket UUID")
            directory_call = payload.hex().startswith("d100")
            route_id = call_route(self.engine.store, payload)
            route = self.engine.store.get("routes", route_id, {})
            integration = self.engine.store.get("integrations", route.get("integration", ""), {})
            if not route.get("enabled") or not integration.get("enabled"):
                raise SpeechError("The phone route is disabled")
            if directory_call or route.get("speech_mode", "legacy") == "legacy":
                port = 9095 if directory_call or payload.hex().startswith("f100") else 9092
                await relay(reader, writer, "127.0.0.1", port, packet(kind, payload))
            else:
                await EngineCall(self.engine, reader, writer).run(handshake)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Provider URLs contain bearer tokens. Never log exception payloads.
            LOG.warning("Speech Engine phone connection ended with an error")
        finally:
            if accepted:
                self.busy = False
            self.tasks.discard(task)
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def start(self):
        self.server = await asyncio.start_server(self.accept, self.host, self.port)

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

"""Extension 0: confirmed Slack directory lookup and an AudioSocket handoff.

Uses the existing voice bridge's shared ASR/TTS models. The private Slack
session remains in the huddle container. No transcript or directory is saved.
"""

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request
import uuid

from audio import FRAME_BYTES, ROOT, packet, read_packet
from pbx import PBX
from speech_gate import SpeechEndpoint

LOG = logging.getLogger("huddle-operator")
CONFIG = Path(os.environ.get("HUDDLE_OPERATOR_CONFIG", str(ROOT / "state/huddle-operator.json")))
GREETING = "Slack operator. Who would you like to huddle with? Say a name, username, or Slack member ID."


def normalized(text):
    return " ".join(re.sub(r"[.!?,]", "", text.lower()).split())


def lookup_query(text):
    text = text.strip(" .!?,")
    text = re.sub(r"^(?:(?:please|can you|could you|i'd like to|i would like to)\s+)*"
                  r"(?:(?:call|huddle with|connect me to|connect to|find)\s+)?", "", text, flags=re.I)
    return re.sub(r"\s+please$", "", text, flags=re.I).strip()


def choice(text):
    text = re.sub(r"^(?:number|option|press)\s+", "", normalized(text))
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
    return int(text) if text in {"1", "2", "3", "4", "5"} else words.get(text)


def confirmed(text):
    return normalized(text) in {"yes", "yes please", "correct", "that's right", "confirm", "call them", "connect", "#"}


class Client:
    def __init__(self, config):
        self.secret = config.get("secret", "")
        self.http_port = int(config.get("http_port", 8099))
        self.audio_port = int(config.get("audio_port", 9094))
        self.owner_id = config.get("owner_id")

    async def request(self, command, **args):
        if os.environ.get("PHONE_PLATFORM_URL"):
            from control import platform_request
            return await platform_request("/api/v1/integrations/slack-huddles/control",
                                          {"command": command, **args})
        def send():
            request = urllib.request.Request(f"http://127.0.0.1:{self.http_port}/control",
                data=json.dumps({"command": command, **args}).encode(),
                headers={"Authorization": "Bearer " + self.secret, "Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.load(response)
        return await asyncio.to_thread(send)


class OperatorCall:
    def __init__(self, bridge, reader, writer):
        self.bridge, self.reader, self.writer = bridge, reader, writer
        self.closed = False
        self.endpoint = SpeechEndpoint()
        self.inputs = asyncio.Queue(maxsize=2)
        self.listening = False
        self.listen_after = 0
        self.relay_writer = None
        self.request_id = None
        self.client = None
        self.directory_number = None
        self.call_id = None
        self.operator_session_id = None

    async def play(self, text):
        self.listening = False
        self.endpoint.reset()
        while not self.inputs.empty():
            self.inputs.get_nowait()
        pcm = await self.bridge.synthesize(text, route="slack-operator")
        deadline = asyncio.get_running_loop().time()
        for offset in range(0, len(pcm), FRAME_BYTES):
            self.writer.write(packet(0x10, pcm[offset:offset + FRAME_BYTES].ljust(FRAME_BYTES, b"\0")))
            await self.writer.drain()
            deadline = max(deadline + .02, asyncio.get_running_loop().time())
            await asyncio.sleep(max(0, deadline - asyncio.get_running_loop().time()))
        self.listen_after = time.monotonic() + .35
        self.listening = True

    async def receive(self):
        audio = bytearray()
        try:
            while True:
                kind, payload = await read_packet(self.reader)
                if kind in (0x00, 0xff):
                    return
                if self.relay_writer:
                    if kind in (0x10, 0x03):
                        self.relay_writer.write(packet(kind, payload))
                        await self.relay_writer.drain()
                    continue
                if not self.listening or time.monotonic() < self.listen_after:
                    audio.clear()
                    continue
                value = None
                if kind == 0x03:
                    value = payload.decode("ascii")
                elif kind == 0x10 and len(payload) % 2 == 0:
                    audio.extend(payload)
                    while len(audio) >= FRAME_BYTES:
                        frame = bytes(audio[:FRAME_BYTES])
                        del audio[:FRAME_BYTES]
                        utterance = self.endpoint.feed(frame)
                        if utterance:
                            value = utterance
                            break
                if value is not None and not self.inputs.full():
                    self.inputs.put_nowait(value)
                    self.listening = False
        finally:
            self.closed = True

    async def text(self):
        value = await asyncio.wait_for(self.inputs.get(), 90)
        self.listening = False
        return (await self.bridge.transcribe(value, route="slack-operator", prompt="Slack huddle operator. Call a person by name or username. "
                                            "Call me. Yes. No. One. Two. Three. Four. Five. Cancel.")
                if isinstance(value, bytes) else value)

    async def handoff(self, user):
        self.request_id = str(uuid.uuid4())
        # No retries of dial. The durable request ID also protects ambiguous
        # HTTP responses; cancellation only ever applies to this operator call.
        await self.client.request("dial", request_id=self.request_id, user_id=user["id"], mode="operator")
        await self.join_huddle(self.request_id)

    async def join_huddle(self, request_id):
        """Join a call already requested by an operator; never dial it twice."""
        self.request_id = request_id
        self.listening = False
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline and not self.closed:
            status = await self.client.request("status", request_id=self.request_id)
            if status["phase"] == "awaiting-handset":
                break
            if status["phase"] != "preparing":
                raise RuntimeError("Huddle preparation failed")
            await asyncio.sleep(.25)
        else:
            raise TimeoutError("Huddle preparation timed out")
        relay_reader, relay_writer = await asyncio.open_connection("127.0.0.1", self.client.audio_port)
        display_task = None
        try:
            relay_writer.write(packet(0x01, uuid.UUID(status["call_id"]).bytes))
            await relay_writer.drain()
            self.relay_writer = relay_writer
            display_task = asyncio.create_task(self.update_huddle_display())
            LOG.info("Operator handset handed to native huddle bridge")
            # The existing reader now forwards handset packets. Speech models
            # stop listening for the entire huddle; this is a raw audio relay.
            while data := await relay_reader.read(65536):
                self.writer.write(data)
                await self.writer.drain()
        finally:
            if display_task:
                display_task.cancel()
                await asyncio.gather(display_task, return_exceptions=True)
            self.relay_writer = None
            relay_writer.close()
            with contextlib.suppress(OSError):
                await relay_writer.wait_closed()

    async def update_huddle_display(self):
        """The operator's audio relay continues even if display updates fail."""
        if not self.call_id:
            return
        try:
            async with asyncio.timeout(30):
                while not self.closed:
                    status = await self.client.request("status", request_id=self.request_id)
                    if status.get("phase") == "connected":
                        display = status.get("display") or {}
                        name = display.get("name") or status.get("display_name") or "Slack huddle"
                        number = display.get("number") or self.directory_number or status.get("user_id")
                        if not self.closed:
                            await PBX().connected_line(self.call_id, name, number)
                        return
                    if status.get("phase") not in {"preparing", "awaiting-handset", "joining"}:
                        return
                    await asyncio.sleep(.25)
        except Exception as error:
            LOG.warning("Huddle display update failed error_type=%s", type(error).__name__)

    async def generic_dialogue(self):
        from control import platform_request
        await self.play(GREETING)
        while not self.closed:
            initial = await self.text()
            if normalized(initial) in {"cancel", "stop", "goodbye", "hang up", "*"}:
                await self.play("Goodbye.")
                return
            if initial:
                break
            await self.play("Please say who you would like to call.")
        if self.closed:
            return
        try:
            instructions = "Help the caller find a Slack contact and connect this live handset. "
            if self.client.owner_id:
                instructions += "When the caller says me or myself, their Slack member ID is " + self.client.owner_id + ". "
            session = await platform_request("/api/v1/operators", {
                "title":"Slack phone operator", "prompt":initial, "instructions":instructions,
                "integration":"slack-huddles", "host":"proxmox", "call_id":self.call_id,
            }, idempotency_key="phone-operator:" + self.call_id)
            self.operator_session_id = session["id"]
            await self.play("One moment.")
            question = None
            announced = None
            while not self.closed:
                handoff = await platform_request("/api/v1/operators/calls/" + self.call_id, method="GET")
                if handoff.get("request_id"):
                    await self.play("Connecting your huddle. Hang up to leave.")
                    await self.join_huddle(handoff["request_id"])
                    return
                state = await platform_request("/api/v1/sessions/" + self.operator_session_id, method="GET")
                pending = [(record, item) for record in state.get("questions", [])
                           if record.get("state", "pending") == "pending"
                           for item in record.get("questions", [])
                           if item["id"] not in record.get("answers", {})]
                if pending:
                    record, item = pending[0]
                    key = (record["id"], item["id"])
                    if question is None or question[0] != key:
                        options = item.get("options", [])
                        spoken = item["question"]
                        if options:
                            spoken += " " + " ".join(f"{index}. {option['label']}." for index, option in enumerate(options, 1))
                        await self.play(spoken)
                        question = (key, options)
                elif state.get("state") in {"done", "error", "interrupted"}:
                    reply = state.get("last_reply") or ("The operator encountered a problem. Please try again." if state["state"] == "error" else "What else would you like to do?")
                    fingerprint = (state["state"], reply)
                    if announced != fingerprint:
                        await self.play(reply)
                        announced = fingerprint
                        question = None
                if not self.inputs.empty():
                    text = await self.text()
                    if normalized(text) in {"cancel", "stop", "goodbye", "hang up", "*"}:
                        await self.play("Goodbye.")
                        return
                    if not text:
                        await self.play("I didn't catch that. Please say it again.")
                    elif question:
                        (question_id, item_id), options = question
                        selected = choice(text)
                        if selected and selected <= len(options):
                            text = options[selected - 1]["label"]
                        await platform_request("/api/v1/questions/" + question_id + "/answer", {"item_id":item_id,"text":text})
                        question = None
                        announced = None
                        self.listening = True
                    else:
                        await platform_request("/api/v1/sessions/" + self.operator_session_id + "/input", {"text":text})
                        announced = None
                        self.listening = True
                await asyncio.sleep(.35)
        except Exception as error:
            LOG.warning("Generic operator error_type=%s", type(error).__name__)
            if not self.closed:
                await self.play("The operator couldn't finish that request. Please hang up and try again.")

    async def dialogue(self):
        if self.directory_number:
            try:
                from control import platform_request
                entry = await platform_request("/api/v1/directories/resolve", {"number": self.directory_number})
                if entry.get("integration") != "slack-huddles":
                    raise ValueError("This directory contact is not a Slack member")
                user = entry["target"]
                if not isinstance(user, dict) or not user.get("user_id"):
                    raise ValueError("This directory contact has no Slack member ID")
                # Selecting Dial in the phone directory is the call request.
                # The directory only stores contacts from actual prior calls.
                await self.play(f"Connecting you to {user.get('name') or user.get('username') or 'your Slack contact'}. "
                                "Hang up to leave the huddle.")
                await self.handoff({**user, "id": user["user_id"]})
            except Exception as error:
                LOG.warning("Directory huddle error_type=%s", type(error).__name__)
                if not self.closed:
                    await self.play("That directory call could not connect. Please hang up and try the Slack operator.")
            return
        if os.environ.get("PHONE_PLATFORM_URL"):
            await self.generic_dialogue()
            return
        await self.play(GREETING)
        while not self.closed:
            text = await self.text()
            if not text:
                await self.play("Please say the person's name again.")
                continue
            if normalized(text) in {"cancel", "stop", "goodbye", "hang up", "*"}:
                await self.play("Goodbye.")
                return
            query = lookup_query(text)
            if normalized(query) in {"me", "myself", "my slack"} and self.client.owner_id:
                query = self.client.owner_id
            try:
                result = await self.client.request("lookup", query=query)
            except Exception as error:
                LOG.warning("Directory lookup error_type=%s", type(error).__name__)
                await self.play("I couldn't look that person up. Try their full name or username.")
                continue
            users = result["users"]
            if not users:
                await self.play("I didn't find an active member with that name. Try another name or username.")
                continue
            if len(users) == 1 and not result["more"]:
                user = users[0]
            else:
                options = " ".join(f"{i}. {u['name']}, username {u['username']}." for i, u in enumerate(users, 1))
                await self.play(("There are more matches than I can read. " if result["more"] else "I found several matches. ")
                                + options + " Say or press a number, or press star to search again.")
                selected = choice(await self.text())
                if not selected or selected > len(users):
                    await self.play("Let's try again. Say a more specific name or username.")
                    continue
                user = users[selected - 1]
            await self.play(f"Call {user['name']}, username {user['username']}? Say yes or press pound to connect. "
                            "Say no or press star to search again.")
            if not confirmed(await self.text()):
                await self.play("No invitation sent. Who would you like to call?")
                continue
            await self.play(f"Connecting you to {user['name']}. Hang up to leave the huddle.")
            self.listening = False
            try:
                await self.handoff(user)
            except Exception as error:
                LOG.warning("Outgoing huddle error_type=%s", type(error).__name__)
                if not self.closed:
                    await self.play("The huddle could not connect. Please hang up and try again.")
            return

    async def run(self):
        kind, payload = await asyncio.wait_for(read_packet(self.reader), 5)
        if kind != 1 or len(payload) != 16:
            raise ValueError("Expected AudioSocket UUID")
        self.call_id = str(uuid.UUID(bytes=payload))
        prefix = payload.hex()[:8]
        if prefix.startswith("d100") and prefix[4:].isdecimal():
            self.directory_number = "88" + prefix[4:]
        if not CONFIG.exists() and not os.environ.get("PHONE_PLATFORM_URL"):
            await self.play("The Slack operator has not been configured yet.")
            return
        self.client = Client(json.loads(CONFIG.read_text()) if CONFIG.exists() else {})
        receiver = asyncio.create_task(self.receive())
        dialogue = asyncio.create_task(self.dialogue())
        try:
            done, _ = await asyncio.wait([receiver, dialogue], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            self.closed = True
            for task in (receiver, dialogue):
                task.cancel()
            await asyncio.gather(receiver, dialogue, return_exceptions=True)
            if os.environ.get("PHONE_PLATFORM_URL") and not self.directory_number:
                from control import platform_request
                with contextlib.suppress(Exception):
                    await platform_request("/api/v1/operators/calls/" + self.call_id, method="DELETE")
            if self.request_id:
                with contextlib.suppress(Exception):
                    await self.client.request("cancel", request_id=self.request_id)

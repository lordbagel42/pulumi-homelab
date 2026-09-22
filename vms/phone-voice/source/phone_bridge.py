#!/usr/bin/env python3
"""Persistent Amp and legacy sessions with detachable telephone audio."""

import asyncio
import audioop
import contextlib
import json
import logging
import os
import re
import signal
import time
import uuid
from collections import deque

import numpy as np

from audio import ROOT, FRAME_BYTES, PCM_RATES, Speech, packet, read_packet
from codex_client import CodexClient
from amp_client import AmpClient
from amp_control import AmpControl
from control import Control, SOCKET
from pbx import PBX
from session_manager import Sessions
from session_store import Store
from speech_gate import SpeechEndpoint

LOG = logging.getLogger("codex-phone")
DIGITS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
HOST_PROMPT = "Where should this task run? Press one or say Proxmox to keep working while this workstation is off. Press two or say workstation to use your local projects."


def host_choice(text):
    normalized = text.lower().strip(" .!?")
    choices = {"1": "proxmox", "one": "proxmox", "option one": "proxmox", "option 1": "proxmox",
               "2": "workstation", "two": "workstation", "option two": "workstation", "option 2": "workstation"}
    if normalized in choices:
        return choices[normalized], ""
    match = re.match(r"^(?:(?:use|on|run (?:it |this |the task )?on) )?(?:(?:the|my) )?"
                     r"(proxmox|prox mox|cluster|workstation|desktop|computer)\b[\s,.:!]*([\s\S]*)$",
                     text, re.IGNORECASE)
    if not match:
        return None, text
    host = "proxmox" if match[1].lower() in {"proxmox", "prox mox", "cluster"} else "workstation"
    rest = match[2].strip()
    if rest.lower().strip(" .!?") == "please":
        rest = ""
    return host, rest


def spoken_number(number, full=False):
    return " ".join(DIGITS[int(d)] for d in (f"611{number:03d}" if full else f"{number:03d}"))


def brief(text, limit=750):
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + ". More details are saved in this session."


class EchoGuard:
    """Suppress strongly correlated playback echo while allowing new speech."""

    def __init__(self):
        self.reference = deque(maxlen=35)
        self.last_sent = 0.0

    def sent(self, frame):
        self.reference.append(frame)
        self.last_sent = time.monotonic()

    def matches(self, frame):
        if not self.reference or time.monotonic() - self.last_sent > 0.7:
            return False
        sample = np.frombuffer(frame, dtype="<i2").astype(np.float32)
        sample -= sample.mean()
        energy = float(np.dot(sample, sample))
        if energy < 1:
            return True
        reference = np.frombuffer(b"".join(self.reference), dtype="<i2").astype(np.float32)
        if len(reference) < len(sample):
            return False
        cumulative = np.concatenate(([0.0], np.cumsum(reference.astype(np.float64) ** 2)))
        windows = cumulative[len(sample):] - cumulative[:-len(sample)]
        correlation = np.abs(np.correlate(reference, sample, mode="valid"))
        scores = correlation / np.sqrt(np.maximum(windows, 1) * energy)
        return bool(np.max(scores) > 0.73)


class Call:
    def __init__(self, bridge, reader, writer):
        self.bridge, self.reader, self.writer = bridge, reader, writer
        self.sessions = bridge.sessions
        self.endpoint, self.echo = SpeechEndpoint(), EchoGuard()
        self.output = asyncio.Queue()
        self.play_task = None
        self.player_task = self.watch_task = None
        self.closed = False
        self.number = None
        self.call_id = None
        self.prompted = None
        self.last_reply = ""
        self.last_error = ""
        self.last_progress = ""
        self.last_user = ""
        self.last_star = 0.0
        self.input_lock = asyncio.Lock()
        self.rx_bytes = self.tx_bytes = self.turn_count = 0
        self.processing = 0
        self.input_generation = 0

    def status(self, event, **extra):
        session = self.sessions.store.get_session(self.number) if self.number else {}
        data = {"event": event, "at": time.time(), "call_id": self.call_id,
                "session_id": self.number, "extension": session.get("extension"),
                "thread_id": session.get("thread_id"), "rx_bytes": self.rx_bytes,
                "host": session.get("host"),
                "tx_bytes": self.tx_bytes, "turns": self.turn_count,
                "last_user": self.last_user, "last_reply": session.get("last_reply", ""), **extra}
        tmp = ROOT / "state/status.tmp"
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        tmp.chmod(0o600)
        tmp.replace(ROOT / "state/status.json")
        LOG.info("%s session=%s call=%s turns=%s rx=%s tx=%s",
                 event, self.number, self.call_id, self.turn_count, self.rx_bytes, self.tx_bytes)

    def say(self, text):
        if not self.closed and text:
            self.output.put_nowait(text)

    def cut_speech(self):
        if self.play_task and not self.play_task.done():
            self.play_task.cancel()
        while not self.output.empty():
            self.output.get_nowait()
            self.output.task_done()
        self.status("interrupted_speech")

    async def play(self, text):
        pcm = await self.bridge.synthesize(text)
        loop = asyncio.get_running_loop()
        deadline = loop.time()
        for offset in range(0, len(pcm), FRAME_BYTES):
            frame = pcm[offset:offset + FRAME_BYTES].ljust(FRAME_BYTES, b"\0")
            self.writer.write(packet(0x10, frame))
            await self.writer.drain()
            self.echo.sent(frame)
            self.tx_bytes += len(frame)
            deadline = max(deadline + 0.02, loop.time() - 0.02)
            await asyncio.sleep(max(0, deadline - loop.time()))

    async def player(self):
        while True:
            text = await self.output.get()
            self.status("speaking", spoken_text=text)
            self.play_task = asyncio.create_task(self.play(text))
            try:
                await self.play_task
            except asyncio.CancelledError:
                if self.closed:
                    raise
            except Exception as error:
                # Keep the queue alive after one provider failure. Previously
                # this killed playback for the rest of an otherwise live call.
                LOG.error("Phone playback failed session=%s error_type=%s", self.number, type(error).__name__)
                self.status("speech_failed", error_type=type(error).__name__)
            finally:
                self.play_task = None
                self.output.task_done()
            self.status("listening")

    def next_question(self):
        for question in self.sessions.store.pending_questions(self.number):
            for item in question["questions"]:
                if item["id"] not in question["answers"]:
                    return question, item
        return None

    def prompt_question(self):
        pending = self.next_question()
        if not pending:
            return
        question, item = pending
        key = (question["id"], item["id"])
        if self.prompted == key:
            return
        self.prompted = key
        text = item["question"]
        for i, option in enumerate(item.get("options") or [], 1):
            text += f" Option {i}: {option['label']}."
        if item.get("options"):
            text += " You can answer in your own words, or press an option number."
        self.say(text)

    async def watch(self):
        while True:
            event = self.sessions.event(self.number)
            generation = self.sessions.input_generation.get(self.number, 0)
            if generation != self.input_generation:
                self.endpoint.reset()
                self.input_generation = generation
            session = self.sessions.store.get_session(self.number)
            progress = self.sessions.spoken_progress.get(self.number, "")
            if progress and progress != self.last_progress:
                self.last_progress = progress
                self.say(brief(progress, 500))
            self.prompt_question()
            error = session["error"] if session["state"] == "error" else ""
            if error and error != self.last_error:
                self.say(self.summary())
            self.last_error = error
            if session["last_reply"] and session["last_reply"] != self.last_reply:
                self.last_reply = session["last_reply"]
                self.turn_count += 1
                self.say(brief(session["last_reply"]))
            await event.wait()

    async def handle_text(self, text, keypad=False):
        text = text.strip()
        if not text:
            return
        LOG.info("Session %03d caller: %s", self.number, text)
        self.last_user = text
        self.status("heard", last_user=text)
        normalized = " ".join(re.sub(r"[^a-z0-9 ]", "", text.lower()).split())
        if (self.play_task and not self.play_task.done()) or not self.output.empty():
            self.cut_speech()
        if normalized in {"stop", "stop talking", "be quiet", "quiet", "pause speaking"}:
            self.cut_speech()
            return
        if normalized in {"stop the task", "stop task", "cancel the task", "cancel task",
                          "stop working", "cancel the current task", "stop the current task",
                          "cancel everything", "stop everything"}:
            self.cut_speech()
            await self.stop_task()
            return
        if normalized in {"hang up", "end the call", "end call", "goodbye"}:
            await self.hangup()
            return
        if normalized in {"whats my number", "what is my number", "repeat the number"}:
            self.say("Call back on " + spoken_number(self.number, full=True) + ".")
            return
        if normalized in {"repeat the question", "repeat question", "say something", "repeat that"}:
            self.prompted = None
            if self.next_question():
                self.prompt_question()
            else:
                self.say(self.summary())
            return
        if self.sessions.store.get_session(self.number)["host"] == "choose":
            host, task = host_choice(text)
            if host:
                self.sessions.select_host(self.number, host)
                self.say(f"This session will run on {host}. " + ("Tell me your task." if not task and not self.sessions.store.next_job(self.number) else ""))
                if task:
                    await self.sessions.submit(self.number, task)
            else:
                self.say(await self.sessions.submit(self.number, text))
            return
        if pending := self.next_question():
            question, item = pending
            options = item.get("options") or []
            choice = normalized.replace("option ", "")
            choice_number = int(choice) if choice.isdecimal() else (
                DIGITS.index(choice) if choice in DIGITS else 0)
            if 1 <= choice_number <= len(options):
                text = options[choice_number - 1]["label"]
            # Give the actual caller's words to Codex. Codex owns conversational
            # clarification and read-back, instead of a fixed yes/no state machine.
            await self.sessions.answer(question["id"], item["id"], text)
            if self.sessions.store.get_session(self.number)["kind"] == "external":
                self.say("I sent your answer back to that session.")
            self.prompted = None
            self.prompt_question()
            return
        acknowledgement = await self.sessions.submit(self.number, text)
        if self.sessions.store.get_session(self.number)["kind"] == "external":
            self.say(acknowledgement)

    async def transcribe(self, pcm, generation):
        self.processing += 1
        try:
            async with self.input_lock:
                text = await self.bridge.transcribe(pcm)
                if generation != self.sessions.input_generation.get(self.number, 0):
                    return
                # Bridge-owned: a task utterance received just before hangup is
                # still submitted. A disconnect without words is never an answer.
                await self.handle_text(text)
        except Exception:
            LOG.exception("Could not handle speech for session %s", self.number)
            self.say("Sorry, I could not process that. Please try again.")
        finally:
            self.processing -= 1

    def launch_utterance(self, pcm):
        if pcm:
            self.bridge.background(self.transcribe(pcm, self.input_generation))

    async def key(self, digit):
        if digit == "*":
            self.cut_speech()
            now = time.monotonic()
            if now - self.last_star < 1.5:
                self.last_star = 0
                await self.stop_task()
            else:
                self.last_star = now
            return
        if digit == "#":
            self.launch_utterance(self.endpoint.finish())
            return
        if digit == "0":
            self.cut_speech()
            self.say("Session " + spoken_number(self.number) + ". " + self.summary())
            return
        if self.next_question() or self.sessions.store.get_session(self.number)["host"] == "choose":
            self.cut_speech()
            await self.handle_text(digit, keypad=True)

    async def stop_task(self):
        try:
            self.say(await self.sessions.interrupt(self.number))
        except (ValueError, RuntimeError):
            LOG.warning("Task cancellation unconfirmed session=%s", self.number)
            self.say("I could not confirm that the task stopped. Check its conversation in Switchboard or Amp before trying again.")

    def summary(self):
        session = self.sessions.store.get_session(self.number)
        if session["host"] == "choose":
            return HOST_PROMPT
        if self.next_question():
            return "I have a question waiting for you."
        if session["state"] in ("running", "waiting"):
            return "The task is still running. " + brief(session["progress"], 400)
        if session["state"] == "interrupted":
            return session["progress"] + " Your conversation is saved."
        if session["state"] == "error":
            return "The task hit an error. " + brief(session["error"], 400)
        return brief(session["last_reply"] or session["progress"] or "Tell me what you would like to work on.")

    async def hangup(self):
        self.writer.write(packet(0x00))
        await self.writer.drain()
        self.writer.close()

    async def run(self):
        kind, payload = await asyncio.wait_for(read_packet(self.reader), 5)
        if kind != 0x01 or len(payload) != 16:
            raise ValueError("Expected AudioSocket UUID handshake")
        call_id = uuid.UUID(bytes=payload)
        self.call_id = str(call_id)
        prefix = call_id.hex[:3]
        if not prefix.isdecimal():
            raise ValueError("Call UUID must begin with its three-digit session code")
        number = int(prefix)
        try:
            session = (self.sessions.create(host="choose" if "workstation" in self.sessions.backends else "proxmox")
                       if number == 0 else self.sessions.store.get_session(number))
        except KeyError:
            await self.play("That session number does not exist. Dial six one one to start a new session.")
            return
        self.number = session["id"]
        self.input_generation = self.sessions.input_generation.get(self.number, 0)
        self.last_reply = session["last_reply"]
        self.last_error = session["error"]
        self.last_progress = self.sessions.spoken_progress.get(self.number, "")
        self.sessions.attach(self.number)
        self.status("connected")
        self.player_task = asyncio.create_task(self.player())
        engine_name = "Amp" if session.get("engine") == "amp" else "Codex"
        greeting = ("This is " + engine_name + " session " + spoken_number(self.number) + ". Call back on "
                    + spoken_number(self.number, full=True) + ". ")
        if session["host"] == "choose":
            greeting += HOST_PROMPT
        elif number == 0:
            greeting += "Tell me your task. Hanging up leaves it running."
        else:
            greeting += (f"This task runs on {session['host']}. " if session["kind"] == "managed" else "") + self.summary()
        self.say(greeting)
        self.watch_task = asyncio.create_task(self.watch())
        audio, rate_state, previous_rate = bytearray(), None, 8000
        try:
            while True:
                kind, payload = await read_packet(self.reader)
                if kind == 0x00:
                    break
                if kind == 0xff:
                    raise RuntimeError("Asterisk reported AudioSocket error")
                if kind == 0x03:
                    await self.key(payload.decode("ascii"))
                    continue
                if kind not in PCM_RATES:
                    continue
                if len(payload) % 2:
                    raise ValueError("Odd-sized PCM frame")
                self.rx_bytes += len(payload)
                rate = PCM_RATES[kind]
                if rate != 8000:
                    if rate != previous_rate:
                        rate_state = None
                    payload, rate_state = audioop.ratecv(payload, 2, 1, rate, 8000, rate_state)
                previous_rate = rate
                audio.extend(payload)
                while len(audio) >= FRAME_BYTES:
                    frame = bytes(audio[:FRAME_BYTES])
                    del audio[:FRAME_BYTES]
                    if self.processing or self.echo.matches(frame):
                        frame = bytes(FRAME_BYTES)
                    pcm = self.endpoint.feed(frame)
                    if (self.endpoint.barge_ready
                            and self.play_task and not self.play_task.done()):
                        self.cut_speech()
                    self.launch_utterance(pcm)
        finally:
            self.closed = True
            self.launch_utterance(self.endpoint.finish())
            self.sessions.detach(self.number)
            for task in (self.play_task, self.player_task, self.watch_task):
                if task:
                    task.cancel()
            await asyncio.gather(*(t for t in (self.play_task, self.player_task, self.watch_task) if t),
                                 return_exceptions=True)
            self.status("disconnected")


class Bridge:
    def __init__(self, speech, sessions):
        self.speech, self.sessions = speech, sessions
        self.active = False
        self.tasks = set()
        self.calls = set()
        self.accept_tasks = set()
        self.synthesis_lock = asyncio.Lock()
        self.transcription_lock = asyncio.Lock()

    def background(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def synthesize(self, text, *, route=None):
        async with self.synthesis_lock:
            kwargs = {"route":route} if route else {}
            task = asyncio.create_task(asyncio.to_thread(self.speech.synthesize, text, **kwargs))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # Piper must finish touching its model before the next utterance
                # acquires the lock, even when playback is interrupted.
                await task
                raise

    async def transcribe(self, pcm, *, prompt=None, route=None):
        async with self.transcription_lock:
            kwargs = {}
            if prompt is not None:
                kwargs["prompt"] = prompt
            if route is not None:
                kwargs["route"] = route
            return await asyncio.to_thread(self.speech.transcribe, pcm, **kwargs)

    async def accept(self, reader, writer, call_type=Call):
        if self.active:
            writer.write(packet(0x00))
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            return
        self.active = True
        call = call_type(self, reader, writer)
        self.calls.add(call)
        self.accept_tasks.add(asyncio.current_task())
        try:
            await call.run()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except Exception:
            LOG.exception("Call failed")
        finally:
            self.active = False
            self.calls.discard(call)
            self.accept_tasks.discard(asyncio.current_task())
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def close(self):
        for call in list(self.calls):
            call.writer.close()
        await asyncio.gather(*list(self.accept_tasks), return_exceptions=True)
        await asyncio.gather(*list(self.tasks), return_exceptions=True)

    async def accept_operator(self, reader, writer):
        from huddle_operator import OperatorCall
        await self.accept(reader, writer, OperatorCall)


async def main():
    state_dir = ROOT / "state"
    state_dir.mkdir(mode=0o700, exist_ok=True)
    state_dir.chmod(0o700)
    (state_dir / "ready").unlink(missing_ok=True)
    store = Store(state_dir / "sessions.sqlite3")
    legacy = state_dir / "status.json"
    if not store.list_sessions() and legacy.exists():
        old = json.loads(legacy.read_text())
        if old.get("thread_id"):
            session = store.create_session("Earlier phone conversation", str(ROOT.parent),
                                           thread_id=old["thread_id"])
            store.update_session(session["id"], last_reply=old.get("last_reply", ""))
    speech = await asyncio.to_thread(Speech)
    codex = CodexClient()
    workstation_socket = os.environ.get("CODEX_PHONE_WORKSTATION_SOCKET")
    workstation = CodexClient(workstation_socket, "workstation") if workstation_socket else None
    amp_controls = AmpControl(store)
    amp = AmpClient(control=amp_controls.request)
    sessions = Sessions(store, codex, PBX(), workstation, amp=amp,
                        default_engine=os.environ.get("PHONE_AGENT_ENGINE", "amp"))
    await sessions.start()
    bridge = Bridge(speech, sessions)
    control = Control(sessions, amp_controls=amp_controls)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        audio_server = await asyncio.start_server(bridge.accept, "127.0.0.1",
                                                 int(os.environ.get("CODEX_AUDIO_PORT", 9092)))
        control_server = await control.start()
        operator_server = await asyncio.start_server(bridge.accept_operator, "127.0.0.1",
                                                     int(os.environ.get("HUDDLE_OPERATOR_PORT", 9095)))
        async with audio_server, control_server, operator_server:
            (state_dir / "ready").write_text("AudioSocket and private control socket ready\n")
            LOG.info("READY: persistent sessions, phone callbacks, and interruptible audio")
            await stop.wait()
    finally:
        (state_dir / "ready").unlink(missing_ok=True)
        SOCKET.unlink(missing_ok=True)
        await bridge.close()
        await sessions.close()
        await codex.close()
        await amp.close()
        if workstation:
            await workstation.close()
        store.db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(main())

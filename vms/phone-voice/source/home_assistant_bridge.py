#!/usr/bin/env python3
"""Dial 555: local speech recognition and playback around Home Assistant Assist."""

import asyncio
import audioop
import contextlib
import json
import logging
import signal
import time
import uuid

from audio import ROOT, FRAME_BYTES, PCM_RATES, Endpoint, Speech, packet, read_packet
from home_assistant import Config, HomeAssistant, HomeAssistantError

LOG = logging.getLogger("home-assistant-phone")
STATE_DIR = ROOT / "state/home-assistant"
GREETING = "Home assistant, what's up?"


class HomeAssistantCall:
    def __init__(self, bridge, reader, writer):
        self.bridge, self.reader, self.writer = bridge, reader, writer
        self.endpoint = Endpoint()
        self.queue = asyncio.Queue(maxsize=1)
        self.busy = True
        self.listen_after = 0.0
        self.call_id = None
        self.worker = None
        self.rx_bytes = self.tx_bytes = self.turn_count = 0

    def status(self, event, **extra):
        data = {"event": event, "at": time.time(), "call_id": self.call_id,
                "rx_bytes": self.rx_bytes, "tx_bytes": self.tx_bytes,
                "turns": self.turn_count, **extra}
        tmp = self.bridge.state_dir / "status.tmp"
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        tmp.chmod(0o600)
        tmp.replace(self.bridge.state_dir / "status.json")
        LOG.info("%s call=%s turns=%s rx=%s tx=%s", event, self.call_id,
                 self.turn_count, self.rx_bytes, self.tx_bytes)

    async def play(self, pcm):
        self.busy = True
        loop = asyncio.get_running_loop()
        deadline = loop.time()
        for offset in range(0, len(pcm), FRAME_BYTES):
            frame = pcm[offset:offset + FRAME_BYTES].ljust(FRAME_BYTES, b"\0")
            self.writer.write(packet(0x10, frame))
            await self.writer.drain()
            self.tx_bytes += len(frame)
            deadline = max(deadline + 0.02, loop.time() - 0.02)
            await asyncio.sleep(max(0, deadline - loop.time()))
        self.listen_after = loop.time() + 0.4
        self.endpoint.reset()
        self.busy = False

    async def say(self, text):
        pcm = await asyncio.to_thread(self.bridge.speech.synthesize, text)
        await self.play(pcm)

    async def dialogue(self):
        conversation_id = None
        try:
            assistant = HomeAssistant(Config.load())
        except HomeAssistantError as error:
            LOG.warning("Home Assistant is not configured: %s", error)
            self.status("not_configured")
            await self.say(
                "Home Assistant isn't connected yet. On the computer, run the Home Assistant "
                "phone setup to enter your server address and access token. Then call 555 again.")
            return
        self.status("connected")
        await self.say(GREETING)
        self.status("listening")
        while True:
            pcm = await self.queue.get()
            self.busy = True
            try:
                text = await asyncio.to_thread(self.bridge.speech.transcribe, pcm)
                if not text:
                    await self.say("I didn't catch that. Please try again.")
                    continue
                self.status("thinking")
                result = await assistant.process(text, conversation_id)
                conversation_id = result.conversation_id or conversation_id
                self.status("speaking", response_type=result.response_type)
                await self.say(result.speech)
                self.turn_count += 1
            except asyncio.CancelledError:
                raise
            except HomeAssistantError as error:
                LOG.warning("Home Assistant request failed: %s", error)
                await self.say(str(error))
            except Exception:
                LOG.exception("Home Assistant call failed")
                await self.say("Something went wrong. I could not confirm the result. Please check the device's status.")
            finally:
                self.endpoint.reset()
                self.busy = False
                self.queue.task_done()
                self.status("listening")

    async def run(self):
        kind, payload = await asyncio.wait_for(read_packet(self.reader), 5)
        if kind != 0x01 or len(payload) != 16:
            raise ValueError("Expected AudioSocket UUID handshake")
        self.call_id = str(uuid.UUID(bytes=payload))
        self.worker = asyncio.create_task(self.dialogue())
        # End the connection even when a finished dialogue receives no more audio.
        self.worker.add_done_callback(lambda _: self.writer.close())
        audio = bytearray()
        rate_state = None
        previous_rate = 8000
        try:
            while True:
                if self.worker.done():
                    self.worker.result()
                    return
                kind, payload = await read_packet(self.reader)
                if kind == 0x00:
                    break
                if kind == 0xff:
                    raise RuntimeError("Asterisk reported AudioSocket error")
                if kind == 0x03:
                    if payload == b"#" and not self.busy:
                        self.enqueue(self.endpoint.finish())
                    continue
                if kind not in PCM_RATES:
                    continue
                if len(payload) % 2:
                    raise ValueError("Odd-sized PCM frame")
                self.rx_bytes += len(payload)
                rate = PCM_RATES[kind]
                if rate != previous_rate:
                    rate_state = None
                if rate != 8000:
                    payload, rate_state = audioop.ratecv(payload, 2, 1, rate, 8000, rate_state)
                previous_rate = rate
                audio.extend(payload)
                while len(audio) >= FRAME_BYTES:
                    frame = bytes(audio[:FRAME_BYTES])
                    del audio[:FRAME_BYTES]
                    if self.busy or time.monotonic() < self.listen_after:
                        self.endpoint.reset()
                        continue
                    self.enqueue(self.endpoint.feed(frame))
        finally:
            try:
                self.worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.worker
            finally:
                self.status("disconnected")

    def enqueue(self, pcm):
        if pcm and not self.queue.full():
            self.busy = True
            self.queue.put_nowait(pcm)


class HomeAssistantBridge:
    def __init__(self, speech):
        self.speech = speech
        self.state_dir = STATE_DIR
        self.active = False

    async def accept(self, reader, writer):
        if self.active:
            writer.write(packet(0x00))
            with contextlib.suppress(ConnectionError):
                await writer.drain()
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            return
        self.active = True
        try:
            await HomeAssistantCall(self, reader, writer).run()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except Exception:
            LOG.exception("Home Assistant call failed")
        finally:
            self.active = False
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()


async def main():
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    ready = STATE_DIR / "ready"
    ready.unlink(missing_ok=True)
    speech = await asyncio.to_thread(
        Speech, GREETING,
        "Home Assistant smart home commands: turn lights on or off, dim lights, "
        "set the temperature, control fans and blinds, activate scenes, and ask device status.")
    bridge = HomeAssistantBridge(speech)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        async with await asyncio.start_server(bridge.accept, "127.0.0.1", 9093):
            ready.write_text("127.0.0.1:9093\n")
            LOG.info("READY: Home Assistant AudioSocket listening on 127.0.0.1:9093 (dial 555)")
            await stop.wait()
    finally:
        ready.unlink(missing_ok=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(main())

#!/usr/bin/env python3
"""Phone audio -> local Whisper -> authenticated Codex -> local Piper -> phone."""

import asyncio
import audioop
import base64
import contextlib
import io
import json
import logging
import os
import re
import signal
import struct
import time
import uuid
import urllib.error
import urllib.request
import wave
from collections import deque
from pathlib import Path

import numpy as np
import webrtcvad
from faster_whisper import WhisperModel
from piper import PiperVoice, SynthesisConfig


ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("codex-phone")
GREETING = "Hi, this is Switchboard. What would you like to work on?"
FRAME_BYTES = 320  # 20 ms, signed little-endian 16-bit mono PCM at 8 kHz.
PCM_RATES = {0x10: 8000, 0x11: 12000, 0x12: 16000, 0x13: 24000,
             0x14: 32000, 0x15: 44100, 0x16: 48000, 0x17: 96000, 0x18: 192000}


def packet(kind, payload=b""):
    if len(payload) > 65535:
        raise ValueError("AudioSocket payload too large")
    return struct.pack("!BH", kind, len(payload)) + payload


async def read_packet(reader):
    kind, size = struct.unpack("!BH", await reader.readexactly(3))
    return kind, await reader.readexactly(size)


class NoSpeechRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SpeechServiceError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"Switchboard speech returned HTTP {status}")


class Endpoint:
    """WebRTC VAD with pre-roll, a silence endpoint, and bounded utterances."""

    def __init__(self):
        self.vad = webrtcvad.Vad(2)
        self.reset()

    def reset(self):
        self.pre = deque(maxlen=12)
        self.frames = []
        self.started = False
        self.run = self.voiced = self.silent = 0

    def feed(self, frame):
        speech = self.vad.is_speech(frame, 8000) and audioop.rms(frame, 2) >= 90
        if not self.started:
            self.pre.append(frame)
            self.run = self.run + 1 if speech else 0
            if self.run >= 3:
                self.started = True
                self.frames = list(self.pre)
                self.voiced = self.run
            return None
        self.frames.append(frame)
        if speech:
            self.voiced += 1
            self.silent = 0
        else:
            self.silent += 1
        if self.silent >= 40 or len(self.frames) >= 1500:
            return self.finish()
        return None

    def finish(self):
        result = b"".join(self.frames) if self.voiced >= 10 else None
        self.reset()
        return result


class Speech:
    def __init__(self, greeting=GREETING, transcription_prompt=None):
        self.transcription_prompt = transcription_prompt or (
            "A conversation with Amp about programming, computers, and a Cisco phone.")
        self.platform_url = os.environ.get("PHONE_PLATFORM_SPEECH_URL", "").rstrip("/")
        self.route = os.environ.get("PHONE_PLATFORM_ROUTE", "codex")
        if self.platform_url:
            self.greeting = self.synthesize(greeting)
            LOG.info("Shared Switchboard speech ready")
            return
        LOG.info("Loading local speech models")
        self.whisper = WhisperModel(
            os.environ.get("WHISPER_MODEL", "small.en"), device="cpu",
            compute_type="int8", cpu_threads=int(os.environ.get("WHISPER_CPU_THREADS", 8)),
            download_root=str(ROOT / "models"))
        self.piper = PiperVoice.load(str(ROOT / "models/en_US-lessac-medium.onnx"))
        self.greeting = self.synthesize(greeting)
        LOG.info("Local speech models ready")

    def platform_request(self, path, data):
        token_file = Path(os.environ.get("PHONE_PLATFORM_TOKEN_FILE", "/var/lib/codex-phone/switchboard/admin-token"))
        token = token_file.read_text().strip()
        request = urllib.request.Request(self.platform_url + "/api/v1/speech/phone/" + path,
            data=json.dumps(data).encode(),
            headers={"Authorization":"Bearer " + token,"Content-Type":"application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoSpeechRedirect())
        try:
            with opener.open(request, timeout=120) as response:
                return response.read(16 * 1024 * 1024)
        except urllib.error.HTTPError as error:
            # Do not log credentials or transcripts from a failed request.
            raise SpeechServiceError(error.code) from None

    def synthesize(self, text, *, route=None):
        if getattr(self, "platform_url", ""):
            payload = {"text": text, "route": route or self.route}
            try:
                result = self.platform_request("synthesize", payload)
            except SpeechServiceError as error:
                if error.status not in {502, 503, 504}:
                    raise
                # A cloud quota/provider failure must not leave the handset
                # silent. The API still checks route and provider enablement.
                LOG.warning("Speech provider unavailable; trying local Piper for route=%s", payload["route"])
                result = self.platform_request("synthesize", {**payload, "provider": "piper"})
            with wave.open(io.BytesIO(result), "rb") as wav:
                if (wav.getnchannels(),wav.getsampwidth(),wav.getframerate()) != (1,2,8000):
                    raise ValueError("Switchboard returned unsupported phone audio")
                return wav.readframes(wav.getnframes())
        text = re.sub(r"\x60{3}.*?\x60{3}", "Code snippet omitted from the spoken response.", text, flags=re.S)
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        text = re.sub(r"[*#\x60]", "", text).strip()
        result = bytearray()
        rate_state = None
        config = SynthesisConfig(length_scale=0.95, volume=0.85)
        for chunk in self.piper.synthesize(text, syn_config=config):
            pcm, rate_state = audioop.ratecv(
                chunk.audio_int16_bytes, 2, 1, chunk.sample_rate, 8000, rate_state)
            result.extend(pcm)
        return bytes(result)

    def transcribe(self, pcm, *, prompt=None, route=None):
        if getattr(self, "platform_url", ""):
            result = self.platform_request("transcribe", {"audio":base64.b64encode(pcm).decode(),
                "sample_rate":8000,"route":route or self.route,"prompt":prompt or self.transcription_prompt})
            return json.loads(result)["text"]
        pcm16, _ = audioop.ratecv(pcm, 2, 1, 8000, 16000, None)
        samples = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0
        segments, _ = self.whisper.transcribe(
            samples, language="en", beam_size=1, temperature=0,
            condition_on_previous_text=False, vad_filter=True,
            initial_prompt=prompt or self.transcription_prompt)
        return " ".join(s.text.strip() for s in segments
                        if s.no_speech_prob < 0.7 and s.avg_logprob > -1.2).strip()

"""One speech interface for local models and ElevenLabs. Audio is not retained."""
import asyncio
import io
import re
import threading
import wave

import httpx


class SpeechError(RuntimeError):
    pass


def elevenlabs_error(body, status_code, fallback):
    """Only expose known error categories, never provider messages or keys."""
    detail = body.get("detail") if isinstance(body, dict) else None
    code = detail.get("status") if isinstance(detail, dict) else None
    if code == "quota_exceeded":
        return "ElevenLabs credits are exhausted; add credits or select local speech"
    if code in {"invalid_api_key", "missing_permissions", "insufficient_permissions"}:
        return "ElevenLabs rejected the API key or its permissions; check the saved integration key"
    if status_code == 429:
        return "ElevenLabs is rate limited; wait before trying speech again"
    if isinstance(status_code, int) and 500 <= status_code < 600:
        return f"ElevenLabs returned a server error (HTTP {status_code}); check provider status before retrying"
    return fallback


def elevenlabs_http_error(error, fallback):
    try:
        body = error.response.json()
    except ValueError:
        body = None
    return elevenlabs_error(body, error.response.status_code, fallback)


class Speech:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.whisper = None
        self.whisper_model = None
        self.piper = None
        self.lock = threading.Lock()
        self.limit = asyncio.Semaphore(2)

    def provider(self, kind, requested=None, route=None):
        settings = self.store.get("settings", "main")
        if route:
            record = self.store.get("routes", route)
            if not record:
                raise SpeechError("Unknown phone route")
            if not record["enabled"]:
                raise SpeechError("This phone route is disabled")
            requested = requested or record[kind]
        id = settings["default_"+kind] if not requested or requested == "default" else requested
        provider = self.store.get("integrations", id)
        if not provider or provider["kind"] != kind or not provider["enabled"]:
            raise SpeechError(f"The selected {kind.upper()} provider is disabled or unavailable")
        return provider

    async def transcribe(self, audio, filename="audio.wav", provider=None, language=None, route=None, prompt=None):
        chosen = self.provider("stt", provider, route)
        async with self.limit:
            if chosen["id"] == "whisper":
                text = await asyncio.to_thread(self._whisper, audio, chosen["config"], language, prompt)
            else:
                key = self.store.credential("elevenlabs-stt")
                if not key:
                    raise SpeechError("Add an ElevenLabs API key in Integrations first")
                fields = {"model_id":chosen["config"]["model_id"], "tag_audio_events":"false", "diarize":"false"}
                if language or chosen["config"].get("language"):
                    fields["language_code"] = language or chosen["config"]["language"]
                try:
                    async with httpx.AsyncClient(timeout=90, trust_env=False, follow_redirects=False) as client:
                        response = await client.post("https://api.elevenlabs.io/v1/speech-to-text", headers={"xi-api-key":key}, data=fields, files={"file":(filename,audio,"application/octet-stream")})
                        response.raise_for_status()
                        text = response.json()["text"]
                except httpx.HTTPStatusError as error:
                    raise SpeechError(elevenlabs_http_error(error,
                        "ElevenLabs transcription failed; check the API key, quota and audio format")) from None
                except (httpx.HTTPError, ValueError, KeyError) as error:
                    raise SpeechError("ElevenLabs transcription failed; check the API key, quota and audio format") from error
        return {"text":text,"provider":chosen["id"]}

    def _whisper(self, audio, config, language, prompt):
        with self.lock:
            try:
                from faster_whisper import WhisperModel
                model = config.get("model", "base.en")
                if self.whisper is None or self.whisper_model != model:
                    self.whisper = WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=4, download_root=str(self.config.models_dir))
                    self.whisper_model = model
                segments, _ = self.whisper.transcribe(io.BytesIO(audio), language=language or config.get("language") or None,
                    beam_size=1, temperature=0, condition_on_previous_text=False, vad_filter=True,
                    initial_prompt=prompt)
                return " ".join(s.text.strip() for s in segments if s.no_speech_prob < .7 and s.avg_logprob > -1.2).strip()
            except ImportError as error:
                raise SpeechError("Local Whisper is not installed on this server") from error
            except Exception as error:
                raise SpeechError("Whisper could not transcribe this audio; check the model and audio format") from error

    async def synthesize(self, text, provider=None, route=None):
        chosen = self.provider("tts", provider, route)
        text = re.sub(r"```.*?```", "Code snippet omitted from the spoken response.", text, flags=re.S)
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        text = re.sub(r"[*#`]", "", text).strip()
        if not text:
            raise SpeechError("Provide text to speak")
        async with self.limit:
            if chosen["id"] == "piper":
                return await asyncio.to_thread(self._piper, text), "audio/wav"
            key = self.store.credential("elevenlabs-tts") or self.store.credential("elevenlabs-stt")
            voice = chosen["config"].get("voice_id")
            if not key or not voice:
                raise SpeechError("Configure an ElevenLabs API key and voice ID first")
            try:
                async with httpx.AsyncClient(timeout=90, trust_env=False, follow_redirects=False) as client:
                    response = await client.post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
                        headers={"xi-api-key":key}, json={"text":text,"model_id":chosen["config"]["model_id"]})
                    response.raise_for_status()
                    return response.content, "audio/mpeg"
            except httpx.HTTPStatusError as error:
                raise SpeechError(elevenlabs_http_error(error,
                    "ElevenLabs speech failed; check the API key, voice and quota")) from None
            except httpx.HTTPError as error:
                raise SpeechError("ElevenLabs speech failed; check the API key, voice and quota") from error

    def _piper(self, text):
        with self.lock:
            try:
                from piper import PiperVoice, SynthesisConfig
                if self.piper is None:
                    self.piper = PiperVoice.load(str(self.config.models_dir / "en_US-lessac-medium.onnx"))
                output = io.BytesIO()
                with wave.open(output, "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(22050)
                    first = True
                    for chunk in self.piper.synthesize(text, syn_config=SynthesisConfig(length_scale=.95, volume=.85)):
                        if first:
                            wav.setframerate(chunk.sample_rate)
                            first = False
                        wav.writeframes(chunk.audio_int16_bytes)
                return output.getvalue()
            except ImportError as error:
                raise SpeechError("Local Piper is not installed on this server") from error
            except Exception as error:
                raise SpeechError("Piper could not synthesize speech; check the installed voice model") from error


def pcm_wav(pcm, rate=8000):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return output.getvalue()


def telephone_wav(audio):
    """Decode any provider's result and resample to signed 8kHz mono for phones."""
    try:
        with wave.open(io.BytesIO(audio)) as wav:
            if (wav.getnchannels(),wav.getsampwidth(),wav.getframerate()) == (1,2,8000):
                return audio
    except (wave.Error, EOFError):
        pass
    try:
        import av
        result = bytearray()
        with av.open(io.BytesIO(audio)) as container:
            resampler = av.AudioResampler(format="s16", layout="mono", rate=8000)
            for frame in container.decode(audio=0):
                for converted in resampler.resample(frame):
                    result.extend(converted.to_ndarray().tobytes())
            for converted in resampler.resample(None):
                result.extend(converted.to_ndarray().tobytes())
        return pcm_wav(bytes(result))
    except ImportError as error:
        raise SpeechError("Phone audio conversion is not installed on this server") from error
    except Exception as error:
        raise SpeechError("The speech provider returned unsupported audio") from error

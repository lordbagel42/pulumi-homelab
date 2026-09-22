"""ElevenLabs Speech Engine control and transcript plane.

Audio connections bind provider conversation IDs to existing owner sessions.
Upstream transcripts never get to choose an agent session. Interruptions cancel
only response delivery; the durable task remains owned by the session bridge.
"""
import asyncio
import contextlib
import hashlib
import math
import re
import time
from urllib.parse import urlsplit

import httpx
import jwt
from fastapi import WebSocketDisconnect
from elevenlabs import AsyncElevenLabs
from elevenlabs.core.api_error import ApiError

from .connectors import ConnectorError
from .speech import SpeechError, elevenlabs_error
from .operator_profiles import profile_for

PROVIDER = "elevenlabs-speech-engine"
ISSUER = "https://api.elevenlabs.io/convai/speech-engine"
SUBJECT = "convai_speech_engine_upstream"


def validate_config(config):
    if set(config) - {"engine_id", "upstream_url", "voice_id", "model_id", "language"}:
        raise ValueError("Unsupported Speech Engine configuration field")
    for key, value in config.items():
        if not isinstance(value, str):
            raise ValueError("Speech Engine configuration values must be text")
        if key == "upstream_url":
            url = urlsplit(value)
            if value and (len(value) > 1000 or url.scheme != "wss" or not url.hostname
                          or url.username or url.password or url.query or url.fragment
                          or url.path != "/speech-engine/upstream"):
                raise ValueError("Use a public wss URL ending in /speech-engine/upstream")
        elif len(value) > 100 or not re.fullmatch(r"[\w.\-]*", value):
            raise ValueError("Invalid Speech Engine configuration")
        elif key == "engine_id" and value and not value.startswith("seng_"):
            raise ValueError("Use a Speech Engine ID beginning with seng_")


def verify_request(headers, key):
    token = headers.get("x-elevenlabs-speech-engine-authorization", "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not key or not token or len(token) > 8192:
        return False
    try:
        claims = jwt.decode(token, hashlib.sha256(key.strip().encode()).digest(),
                            algorithms=["HS256"], issuer=ISSUER, subject=SUBJECT,
                            leeway=60, options={"require": ["exp", "iat", "iss", "sub"]})
        return all(type(claims[k]) in (int, float) and math.isfinite(claims[k]) for k in ("exp", "iat"))
    except (jwt.PyJWTError, ValueError, TypeError, OverflowError):
        return False


def pending_question(session):
    for question in session.get("questions", []):
        if question.get("state") in {"answered", "cancelled", "expired"}:
            continue
        for item in question.get("questions", []):
            if item["id"] not in question.get("answers", {}):
                return question, item
    return None


def speakable(text):
    text = re.sub(r"```.*?```", "Code is available in the session.", text, flags=re.S)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return re.sub(r"[*#`]", "", text).strip()


def question_text(item):
    result = item["question"]
    for i, option in enumerate(item.get("options") or [], 1):
        result += f" Option {i}: {option['label']}."
    return result


class SessionReply:
    def __init__(self, connector, session_id, poll_interval=.25, bootstrap_token=None, greeting="", instructions=""):
        self.connector, self.session_id = connector, session_id
        self.poll_interval = poll_interval
        self.bootstrap_token, self.greeting = bootstrap_token, greeting
        self.instructions = instructions
        self.submit_lock = asyncio.Lock()
        self.submissions = set()
        self.last_event = -1

    async def respond(self, transcript):
        if not transcript or transcript[-1].get("role") != "user":
            return
        text = transcript[-1].get("content", "").strip()
        if not text or len(text) > 20000:
            raise SpeechError("Invalid Speech Engine transcript")
        normalized = " ".join(re.sub(r"[^a-z0-9 ]", "", text.lower()).split())
        if normalized in {"stop", "stop talking", "be quiet", "quiet", "pause speaking", "hang up", "end the call", "end call", "goodbye"}:
            return
        if normalized in {"stop the task", "stop task", "cancel the task", "cancel task", "stop working", "stop everything", "cancel everything"}:
            result = await self.connector.cancel(self.session_id)
            yield result.get("message", "Task stop requested.")
            return
        if normalized in {"whats my number", "what is my number", "repeat the number"}:
            state = await self.connector.get_session(self.session_id)
            yield "Call back on " + " ".join(state["extension"]) + "."
            return
        before = await self.connector.get_session(self.session_id)
        if normalized in {"repeat the question", "repeat question", "repeat that"}:
            pending = pending_question(before)
            yield question_text(pending[1]) if pending else before.get("last_reply") or "Tell me your task."
            return
        bootstrap = bool(self.bootstrap_token and text == self.bootstrap_token)
        host_selected = False
        if before.get("host") == "choose" and not bootstrap:
            choice = normalized.removeprefix("option ")
            host = {"1": "proxmox", "one": "proxmox", "proxmox": "proxmox",
                    "2": "workstation", "two": "workstation", "workstation": "workstation"}.get(choice)
            if host:
                await self.connector.request("select_host", session_id=self.session_id, host=host)
                host_selected = True
                yield "This session will run on " + host + "."
            else:
                yield "Choose one for Proxmox or two for the workstation. Your saved task will remain queued."
                return
        if bootstrap:
            yield self.greeting
        elif not host_selected:
            # Shield a submitted mutation from an audio interruption. Serialize it
            # with the next turn so an interrupted socket write cannot reorder input.
            async def submit():
                async with self.submit_lock:
                    state = await self.connector.get_session(self.session_id)
                    pending = pending_question(state)
                    if pending:
                        question, item = pending
                        answer = text
                        choice = normalized.removeprefix("option ")
                        digits = ["zero", "one", "two", "three", "four", "five", "six"]
                        n = int(choice) if choice.isdecimal() else digits.index(choice) if choice in digits else 0
                        options = item.get("options") or []
                        if 1 <= n <= len(options):
                            answer = options[n-1]["label"]
                        await self.connector.answer_question(self.session_id, question["id"], answer, item["id"])
                    else:
                        prompt = ("Operator instructions:\n" + self.instructions + "\n\nCaller:\n" + text) if self.instructions else text
                        await self.connector.send_input(self.session_id, prompt)
            task = asyncio.create_task(submit())
            self.submissions.add(task)
            task.add_done_callback(self.submissions.discard)
            # Retrieve exceptions even when the response is interrupted.
            task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
            await asyncio.shield(task)
        last_reply, last_progress = before.get("last_reply"), before.get("progress")
        last_completed = before.get("updated") if before.get("state") == "done" else None
        pending = pending_question(before)
        question_seen = (pending[0]["id"], pending[1]["id"]) if bootstrap and pending else None
        # Leave the response open while a task is working, including while a
        # question waits for the owner. This delivers progress without synthetic
        # user messages that could accidentally start more work.
        while True:
            state = await self.connector.get_session(self.session_id)
            pending = pending_question(state)
            if pending:
                question, item = pending
                key = (question["id"], item["id"])
                if key != question_seen:
                    question_seen = key
                    yield question_text(item)
            reply = state.get("last_reply")
            completed = state.get("updated") if state.get("state") == "done" else None
            if reply and (reply != last_reply or (completed is not None and completed != last_completed)):
                last_reply = reply
                last_completed = completed
                yield reply
            progress = state.get("progress")
            if progress and progress != last_progress and progress != reply and state.get("state") == "running":
                last_progress = progress
                yield progress
            if state.get("state") == "error":
                yield "The task encountered an error. Check the saved session for details."
                return
            await asyncio.sleep(self.poll_interval)


class SpeechEngine:
    def __init__(self, store, connector, transport=None):
        self.store, self.connector, self.transport = store, connector, transport
        self.bindings = {}
        self.active_upstreams = set()
        self.upstream_sockets = {}
        self.provision_lock = asyncio.Lock()

    def settings(self, require_engine=True, route=None):
        record = self.store.get("integrations", PROVIDER)
        if not record or not record["enabled"]:
            raise SpeechError("Enable ElevenLabs Speech Engine in Integrations first")
        config = dict(record["config"])
        if route:
            if route not in {"codex", "slack-operator"}:
                raise ValueError("Speech Engine supports the agent and Slack operator routes")
            profile = profile_for(self.store, route)
            for name in ("voice_id", "model_id", "language"):
                config[name] = profile[name] or config.get(name, "")
            # Each route owns a resource so updates cannot change another voice.
            config["engine_id"] = profile["engine_id"]
        key = self.store.credential(PROVIDER)
        if not key or (require_engine and not config.get("engine_id")):
            raise SpeechError("Configure a Speech Engine API key and engine ID first")
        return config, key

    @contextlib.asynccontextmanager
    async def provider_client(self):
        _, key = self.settings(require_engine=False)
        try:
            async with httpx.AsyncClient(timeout=20, trust_env=False, follow_redirects=False,
                                         transport=self.transport) as client:
                yield AsyncElevenLabs(api_key=key, httpx_client=client, timeout=20)
        except ApiError as error:
            raise SpeechError(elevenlabs_error(error.body, error.status_code,
                "Speech Engine request failed; check configuration and account usage before retrying")) from None
        except (httpx.HTTPError, ValueError, AttributeError):
            raise SpeechError("Speech Engine request failed; check configuration and account usage before retrying") from None

    async def provision(self, route=None):
        async with self.provision_lock:
            config, _ = self.settings(require_engine=False, route=route)
            validate_config(config)
            if not config.get("upstream_url") or not config.get("voice_id"):
                raise ValueError("Set the public upstream URL and voice ID first")
            payload = {"name": "Switchboard", "speech_engine": {"ws_url": config["upstream_url"]},
                       "asr": {"user_input_audio_format": "pcm_16000"},
                       "tts": {"voice_id": config["voice_id"], "model_id": config["model_id"],
                               "agent_output_audio_format": "pcm_16000"},
                       "language": config.get("language", "en"),
                       "overrides": {"first_message": True},
                       "conversation": {"client_events": ["audio", "interruption", "user_transcript", "agent_response", "agent_response_complete"]},
                       "privacy": {"record_voice": False, "delete_audio": True, "delete_transcript_and_pii": True}}
            if route:
                profile = profile_for(self.store, route)
                payload["name"] = "Switchboard — " + self.store.get("routes", route)["name"]
                for section in ("tts", "asr", "turn", "conversation"):
                    payload.setdefault(section, {}).update(profile[section])
                for name in ("speed", "stability", "similarity_boost"):
                    if profile[name] is not None:
                        payload["tts"][name] = profile[name]
            engine_id = config.get("engine_id")
            others = [r.get("speech_engine", {}).get("engine_id") for r in self.store.all("routes") if r["id"] != route]
            if route:
                others.append(self.store.get("integrations", PROVIDER)["config"].get("engine_id"))
            if engine_id and engine_id in others:
                raise ValueError("Each operator must use its own Speech Engine resource")
            pending_id = "provision-pending" + (":" + route if route else "")
            if not engine_id:
                if self.store.get("speech-engine", pending_id):
                    raise SpeechError("A prior creation could not be confirmed. Find its Speech Engine ID in ElevenLabs and save it before retrying")
                self.store.put("speech-engine", pending_id, {"started": time.time()})
            # Disable SDK retries: a lost creation response must not create a
            # second billable resource. The pending marker survives failures.
            async with self.provider_client() as client:
                options = {"max_retries": 0}
                if engine_id:
                    # SDK 2.68 exposes overrides on create only. Its supported
                    # body extension preserves the startup override on update.
                    options["additional_body_parameters"] = {"overrides": payload.pop("overrides")}
                    result = await client.speech_engine.update(engine_id, **payload, request_options=options)
                else:
                    result = await client.speech_engine.create(**payload, request_options=options)
            engine_id = getattr(result.config, "speech_engine_id", None)
            if not isinstance(engine_id, str) or not re.fullmatch(r"seng_[\w-]+", engine_id):
                raise SpeechError("Speech Engine returned an invalid ID; inspect the provider dashboard before retrying")
            if route:
                record = self.store.get("routes", route)
                record.setdefault("speech_engine", {})["engine_id"] = engine_id
                self.store.put("routes", route, record)
            else:
                record = self.store.get("integrations", PROVIDER)
                record["config"]["engine_id"] = engine_id
                self.store.put("integrations", PROVIDER, record)
            self.store.delete("speech-engine", pending_id)
            return {"engine_id": engine_id}

    async def signed_url(self, route=None):
        config, _ = self.settings(route=route)
        async with self.provider_client() as client:
            data = await client.conversational_ai.conversations.get_signed_url(
                agent_id=config["engine_id"], request_options={"max_retries": 0})
        url = getattr(data, "signed_url", "")
        if not isinstance(url, str):
            raise SpeechError("Speech Engine returned an invalid audio connection")
        parsed = urlsplit(url)
        if parsed.scheme != "wss" or parsed.hostname != "api.elevenlabs.io" or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise SpeechError("Speech Engine returned an invalid audio connection")
        return url

    def bind(self, conversation_id, reply):
        if not isinstance(conversation_id, str) or not re.fullmatch(r"[\w-]{1,200}", conversation_id) or conversation_id in self.bindings:
            raise SpeechError("Invalid or duplicate Speech Engine conversation")
        self.bindings[conversation_id] = reply

    async def unbind(self, conversation_id):
        self.bindings.pop(conversation_id, None)
        websocket = self.upstream_sockets.get(conversation_id)
        if websocket:
            with contextlib.suppress(RuntimeError, OSError):
                await websocket.close()

    async def upstream(self, websocket):
        try:
            _, key = self.settings(require_engine=False)
        except SpeechError:
            await websocket.close(code=1008)
            return
        if not verify_request(websocket.headers, key):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        response_task = None
        conversation_id = None
        send_lock = asyncio.Lock()

        async def send(message):
            async with send_lock:
                await websocket.send_json(message)

        async def respond(reply, transcript, event_id):
            try:
                async for text in reply.respond(transcript):
                    text = speakable(text)
                    if text:
                        await send({"type": "agent_response", "event_id": event_id, "content": text+" ", "is_final": False})
                await send({"type": "agent_response", "event_id": event_id, "content": "", "is_final": True})
            except asyncio.CancelledError:
                raise
            except (SpeechError, ConnectorError, ValueError):
                await send({"type": "agent_response", "event_id": event_id,
                            "content": "The task connection failed. Check session state before repeating an instruction.", "is_final": False})
                await send({"type": "agent_response", "event_id": event_id, "content": "", "is_final": True})

        try:
            async with asyncio.timeout(20):
                init = await websocket.receive_json()
                if not isinstance(init, dict) or init.get("type") != "init" or not isinstance(init.get("conversation_id"), str):
                    raise ValueError("Expected Speech Engine initialization")
                conversation_id = init["conversation_id"]
                if conversation_id in self.active_upstreams:
                    raise ValueError("Duplicate upstream")
                # The provider may open upstream before audio metadata arrives.
                while conversation_id not in self.bindings:
                    await asyncio.sleep(.02)
                if conversation_id in self.active_upstreams:
                    raise ValueError("Duplicate upstream")
                self.active_upstreams.add(conversation_id)
                self.upstream_sockets[conversation_id] = websocket
                reply = self.bindings[conversation_id]
            while True:
                message = await websocket.receive_json()
                if not isinstance(message, dict):
                    raise ValueError("Invalid upstream message")
                kind = message.get("type")
                if kind == "ping":
                    await send({"type": "pong"})
                elif kind in {"close", "error"}:
                    break
                elif kind == "user_transcript":
                    event = message.get("event_id")
                    transcript = message.get("user_transcript")
                    if type(event) is not int or event < 0 or not isinstance(transcript, list) or not transcript or len(transcript) > 2000:
                        raise ValueError("Invalid transcript event")
                    if any(not isinstance(m, dict) or m.get("role") not in {"user", "agent"} or not isinstance(m.get("content"), str) or len(m["content"]) > 20000 for m in transcript):
                        raise ValueError("Invalid transcript message")
                    if event <= reply.last_event:
                        continue
                    reply.last_event = event
                    if response_task:
                        response_task.cancel()
                        await asyncio.gather(response_task, return_exceptions=True)
                    response_task = asyncio.create_task(respond(reply, transcript, event))
        except (WebSocketDisconnect, TimeoutError, ValueError, KeyError, TypeError, RuntimeError):
            pass
        finally:
            if response_task:
                response_task.cancel()
                await asyncio.gather(response_task, return_exceptions=True)
            if conversation_id and 'reply' in locals():
                self.active_upstreams.discard(conversation_id)
                self.upstream_sockets.pop(conversation_id, None)
            with contextlib.suppress(RuntimeError, OSError):
                await websocket.close()

"""Authenticated shared API. The dashboard and integrations use these same routes."""
import asyncio
import base64
import contextlib
import hashlib
import html
import importlib.util
import json
import logging
import math
import os
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode, urlsplit
import xml.etree.ElementTree as ET

import httpx
from fastapi import FastAPI, Request, Response, HTTPException, UploadFile, File, Form, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .attention import DEFAULT_QUIET, is_quiet
from . import __version__
from .config import Config
from .connectors import CodexConnector, HuddleConnector, ConnectorError
from .speech import Speech, SpeechError, pcm_wav, telephone_wav
from .store import Store, ROUTES
from .speech_engine import SpeechEngine, PROVIDER as ENGINE_PROVIDER, validate_config as validate_engine_config
from .speech_engine_media import AudioSocketServer
from .operator_profiles import OperatorProfile

LOG = logging.getLogger("switchboard")
API = "/api/v1"
MAX_AUDIO = 25 * 1024 * 1024


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Model):
    token: str = Field(min_length=1, max_length=512)


class Operator(Model):
    title: str = Field(default="Operator task", min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=20000)
    host: Literal["proxmox", "workstation"] | None = None
    cwd: str | None = Field(default=None, max_length=1000)
    instructions: str = Field(default="", max_length=10000)
    integration: str = Field(default="amp", pattern=r"^[a-z][a-z0-9-]{0,63}$")
    engine: Literal["amp", "codex"] | None = None
    call_id: uuid.UUID | None = None


class Registration(Model):
    title: str = Field(min_length=1, max_length=200)
    integration: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    external_key: str = Field(min_length=1, max_length=200)
    metadata: dict = Field(default_factory=dict)


class Text(Model):
    text: str = Field(min_length=1, max_length=20000)


class Callback(Model):
    question: str | None = Field(default=None, max_length=3000)
    message: str | None = Field(default=None, max_length=3000)
    options: list[str] = Field(default_factory=list, max_length=6)


class Answer(Model):
    item_id: str = Field(default="answer", min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=10000)


class Update(Model):
    summary: str = Field(max_length=5000)
    state: Literal["idle", "running", "waiting", "done", "error", "interrupted"] = "running"


class IntegrationUpdate(Model):
    enabled: bool | None = None
    config: dict | None = None
    api_key: str | None = Field(default=None, max_length=1000)


class Settings(Model):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    default_stt: Literal["whisper", "elevenlabs-stt"] | None = None
    default_tts: Literal["piper", "elevenlabs-tts"] | None = None
    default_host: Literal["proxmox", "workstation"] | None = None


class QuietHours(Model):
    enabled: bool = False
    start: str = Field(default="22:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(default="08:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    timezone: str = Field(default="America/Boise", max_length=100)


class TaskTemplate(Operator):
    number: str = Field(pattern=r"^89\d{4}$")
    enabled: bool = True


class Route(Model):
    name: str = Field(min_length=1, max_length=80)
    extension: str = Field(pattern=r"^\d{1,8}$")
    integration: Literal["amp", "codex", "slack-huddles", "home-assistant"]
    stt: Literal["default", "whisper", "elevenlabs-stt"] = "default"
    tts: Literal["default", "piper", "elevenlabs-tts"] = "default"
    enabled: bool = True
    speech_mode: Literal["legacy", "speech-engine"] = "legacy"
    speech_engine: OperatorProfile = Field(default_factory=OperatorProfile)
    id: str | None = None


class Directory(Model):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    name: str = Field(min_length=1, max_length=60)
    integration: str = Field(default="custom", pattern=r"^[a-z][a-z0-9-]{0,63}$")
    enabled: bool = True


class DirectoryUpdate(Model):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    enabled: bool | None = None


class DirectoryEntry(Model):
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=200)
    number: str = Field(pattern=r"^\d{1,16}$")


class Entries(Model):
    entries: list[DirectoryEntry] = Field(max_length=500)


class Favorites(Entries):
    revision: int = Field(ge=0)


class Synthesis(Text):
    provider: str | None = None
    route: str | None = None


class Conversation(Text):
    conversation_id: str | None = Field(default=None, max_length=300)


class PhoneAudio(Model):
    audio: str = Field(max_length=6*1024*1024)
    sample_rate: Literal[8000] = 8000
    route: str = "codex"
    prompt: str | None = Field(default=None, max_length=1000)


def read_private(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def create_app(config=None, connector=None, speech=None):
    config = config or Config()
    store = Store(config.state_dir)
    codex = connector or CodexConnector(config.voice_socket)
    speech = speech or Speech(config, store)
    engine = SpeechEngine(store, codex)
    engine_audio = AudioSocketServer(engine, config.speech_engine_host, config.speech_engine_port, config.speech_engine_peers)
    started = time.time()
    snapshots = {}
    login_failures = {}
    health_cache = {"time":0, "items":[]}
    operator_lock = asyncio.Lock()
    route_lock = asyncio.Lock()
    huddle_dial_lock = asyncio.Lock()
    recent_name_retry = {}
    callback_lock = asyncio.Lock()

    def huddle():
        private = read_private(config.huddle_config)
        token = store.credential("slack-huddles") or private.get("secret", "")
        return HuddleConnector(f'http://127.0.0.1:{int(private.get("http_port", 8099))}', token)

    engine.huddle_factory = huddle
    engine.huddle_audio_port = int(read_private(config.huddle_config).get("audio_port", 9094))
    engine.huddle_owner_id = read_private(config.huddle_config).get("owner_id", "")

    def enabled(id):
        record = store.get("integrations", id)
        if not record or not record["enabled"]:
            raise HTTPException(409, "This integration is disabled")
        return record

    def normalize(session):
        engine = session.get("engine") or "codex"
        result = {**session, "id":"codex:"+str(session["id"]), "engine":engine, "integration":engine}
        meta = store.get("session-meta", result["id"], {})
        return {**result, **meta}

    async def sessions():
        return [normalize(s) for s in await codex.list_sessions()]

    async def integrations():
        if time.monotonic()-health_cache["time"] < 4:
            return health_cache["items"]
        codex_health, slack_health = await asyncio.gather(codex.health(), huddle().health())
        items = []
        for record in store.all("integrations"):
            id = record["id"]
            item = {**record,"key_configured":bool(store.credential(id)),"status":"ready","detail":"Configured"}
            if not record["enabled"]:
                item.update(status="disabled",detail="Disabled")
            elif id in {"amp", "codex"}:
                item.update({k:codex_health[k] for k in ("status","detail")})
                capabilities = codex_health.get("capabilities", {})
                item["backends"] = [b for b in capabilities.get("backends", []) if b.get("engine") == id]
                if id == "amp" and codex_health["healthy"]:
                    if not any(b.get("configured") for b in item["backends"]):
                        item.update(status="needs_configuration", detail="Phone bridge connected; configure an Amp runner to start tasks")
                    else:
                        item.update(status="configured", detail="Amp runner configured; threads open in Amp for review")
            elif id == "slack-huddles":
                item.update({k:slack_health[k] for k in ("status","detail")})
                item["key_configured"] = slack_health.get("control_configured", False)
                if not item["key_configured"]:
                    item.update(status="needs_configuration",detail="Operator control credentials are not configured")
            elif id == "home-assistant":
                item["key_configured"] = bool(read_private(config.home_assistant_config).get("token"))
                item.update(status="configured" if item["key_configured"] else "needs_configuration",detail="Uses the existing Home Assistant phone connection" if item["key_configured"] else "Home Assistant connection is missing")
            elif id == "whisper":
                ready = bool(importlib.util.find_spec("faster_whisper")) and config.models_dir.exists()
                item.update(status="ready" if ready else "unavailable",detail="Local CPU recognition" if ready else "Local speech runtime is not installed")
            elif id == "piper":
                ready = (config.models_dir / "en_US-lessac-medium.onnx").exists()
                item.update(status="ready" if ready else "unavailable",detail="Local speech synthesis" if ready else "Piper voice is not installed")
            elif id == ENGINE_PROVIDER:
                resources = record["config"].get("engine_id") or any(r.get("speech_engine", {}).get("engine_id") for r in store.all("routes"))
                missing = not item["key_configured"] or not resources or not record["config"].get("upstream_url")
                item.update(status="needs_configuration" if missing else "configured",
                            detail="Set the API key, Speech Engine ID and public upstream URL" if missing else "Continuous speech configured; each phone route chooses its speech mode")
            elif id.startswith("elevenlabs"):
                if id == "elevenlabs-tts":
                    item["key_configured"] = bool(store.credential(id) or store.credential("elevenlabs-stt"))
                if not item["key_configured"] or (id.endswith("tts") and not record["config"].get("voice_id")):
                    item.update(status="needs_configuration", detail="Add an API key" if not item["key_configured"] else "Choose an ElevenLabs voice ID")
                else:
                    item.update(status="configured", detail="Cloud provider configured; usage is billed by ElevenLabs")
            items.append(item)
        health_cache.update(time=time.monotonic(),items=items)
        return items

    async def sync_recent_huddles():
        record = store.get("integrations","slack-huddles")
        if not record or not record["enabled"]:
            return
        client = huddle()
        history = await client.recent()
        for item in history.get("contacts",[]):
            user_id, connected_at = item.get("user_id"),item.get("connected_at")
            if (not isinstance(user_id,str) or not re.fullmatch(r"[UW][A-Z0-9]+",user_id)
                    or not isinstance(connected_at,(int,float)) or not math.isfinite(connected_at) or connected_at <= 0):
                continue
            contact = store.get("slack-users",user_id,{})
            store.contact(user_id,contact.get("name") or user_id,contact.get("username", ""),connected_at)
            if contact:
                store.contact_name(user_id,contact.get("name") or user_id,contact.get("username", ""))
            elif time.monotonic() >= recent_name_retry.get(user_id,0):
                # Exact-ID lookup is read-only and outside the live media path.
                # A transient directory failure keeps a visible ID and retries.
                recent_name_retry[user_id] = time.monotonic()+60
                try:
                    found = await client.lookup(user_id)
                    contact = next((user for user in found.get("users",[]) if user.get("id") == user_id),None)
                    if contact:
                        store.put("slack-users",user_id,contact)
                        store.contact_name(user_id,contact.get("name") or user_id,contact.get("username", ""))
                except ConnectorError:
                    pass

    async def monitor():
        last_recent = 0
        while True:
            try:
                try:
                    current = await sessions()
                except ConnectorError:
                    current = []
                for session in current:
                    fingerprint = (session["state"],session.get("progress"),session.get("last_reply"))
                    if session["id"] in snapshots and snapshots[session["id"]] != fingerprint:
                        store.event("session.updated", session.get("progress") or session.get("last_reply") or session["state"], session["id"])
                    snapshots[session["id"]] = fingerprint
                await dispatch_callbacks()
                for pending in store.all("pending-huddles"):
                    if time.time()-pending["created"] > 600:
                        store.delete("pending-huddles", pending["request_id"])
                        continue
                    state = await huddle().status(pending["request_id"])
                    if state.get("phase") == "connected":
                        contact = store.get("slack-users", pending["user_id"], {"name":pending["user_id"]})
                        store.delete("pending-huddles", pending["request_id"])
                        store.event("huddle.connected", "Connected to "+contact.get("name",pending["user_id"]))
                    elif state.get("phase") in ("failed","ended","cancelled","idle","finished","time-limit","interrupted"):
                        store.delete("pending-huddles", pending["request_id"])
            except (ConnectorError, OSError, ValueError):
                pass
            except Exception:
                LOG.exception("Status monitor failed")
            if time.monotonic()-last_recent >= 10:
                last_recent = time.monotonic()
                try:
                    await sync_recent_huddles()
                except (ConnectorError,OSError,ValueError):
                    pass
                except Exception:
                    LOG.exception("Recent huddle sync failed")
            await asyncio.sleep(2)

    @asynccontextmanager
    async def lifespan(app):
        if config.speech_engine_port:
            await engine_audio.start()
        task = asyncio.create_task(monitor())
        store.event("platform.started", "Switchboard started")
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await engine_audio.close()
            store.close()

    app = FastAPI(title="Switchboard API", version=__version__, description="Shared phone sessions, operators, speech and optional directories.", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url="/api/openapi.json")
    app.state.store, app.state.config = store, config
    app.state.sync_recent_huddles = sync_recent_huddles
    app.state.speech_engine = engine

    @app.middleware("http")
    async def boundary(request, call_next):
        path = request.url.path
        is_api = path.startswith("/api/")
        if is_api and path != API+"/auth/login":
            bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
            authorized = bool(bearer) and secrets.compare_digest(bearer.encode(), store.admin_token.encode())
            cookie = request.cookies.get("switchboard_session", "")
            if not authorized and not (cookie and store.valid_login(cookie)):
                return JSONResponse({"detail":"Sign in to Switchboard"},status_code=401)
        if is_api and request.method not in ("GET","HEAD","OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail":"Cross-origin writes are not allowed"},status_code=403)
            length = request.headers.get("content-length", "0")
            if not length.isdecimal() or int(length) > MAX_AUDIO+1024*1024:
                return JSONResponse({"detail":"Request is too large"},status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store" if is_api or path.startswith("/phone/") else "no-cache"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; media-src 'self' blob:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    @app.exception_handler(ConnectorError)
    async def connector_error(request, error):
        return JSONResponse({"detail":str(error)},status_code=error.status_code)

    @app.exception_handler(SpeechError)
    async def speech_error(request, error):
        return JSONResponse({"detail":str(error)},status_code=503)

    @app.exception_handler(ValueError)
    async def value_error(request, error):
        return JSONResponse({"detail":str(error)},status_code=400)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # Do not echo submitted keys, transcripts or other input in errors.
        return JSONResponse({"detail":"; ".join(".".join(map(str,e["loc"]))+": "+e["msg"] for e in error.errors())},status_code=422)

    @app.get("/healthz")
    async def health():
        return {"healthy":True,"service":"switchboard","version":__version__}

    @app.post(API+"/auth/login")
    async def login(body: Login, request: Request):
        key = request.client.host if request.client else "unknown"
        attempts = [t for t in login_failures.get(key,[]) if time.time()-t < 60]
        if len(attempts) >= 10:
            raise HTTPException(429,"Too many sign-in attempts; wait one minute")
        if not secrets.compare_digest(body.token.encode(),store.admin_token.encode()) and not store.valid_login(body.token,"link",True):
            login_failures[key] = attempts + [time.time()]
            raise HTTPException(401,"Invalid or expired sign-in token")
        login_failures.pop(key,None)
        response = JSONResponse({"authenticated":True})
        response.set_cookie("switchboard_session",store.login(),httponly=True,samesite="strict",secure=request.url.scheme=="https",max_age=43200)
        return response

    @app.get(API+"/auth/session")
    async def auth_session():
        return {"authenticated":True}

    @app.post(API+"/auth/logout")
    async def logout(request: Request):
        store.revoke_login(request.cookies.get("switchboard_session", ""))
        response = JSONResponse({"authenticated":False})
        response.delete_cookie("switchboard_session")
        return response

    @app.get(API+"/overview")
    async def overview():
        try:
            current = await sessions()
        except ConnectorError:
            current = []
        return {"service":{"name":store.get("settings","main")["name"],"version":__version__,"uptime":time.time()-started},"integrations":await integrations(),"sessions":current,"events":store.events(limit=30),"settings":store.get("settings","main")}

    @app.get(API+"/sessions")
    async def list_sessions():
        return {"sessions":await sessions()}

    @app.post(API+"/sessions", status_code=201)
    async def register(body: Registration):
        enabled("codex")
        record = await codex.register_session(body.integration+":"+body.external_key,body.title)
        id = "codex:"+str(record["id"])
        store.put("session-meta",id,{"integration":body.integration,"metadata":body.metadata})
        store.event("session.registered",body.title,id)
        return normalize(record)

    async def start_operator_locked(body, request):
        engine = body.engine or ("codex" if body.integration == "codex" else "amp")
        enabled(engine)
        idem = request.headers.get("idempotency-key")
        fingerprint = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
        if idem:
            if len(idem) > 200 or not idem.strip():
                raise HTTPException(400,"Use an idempotency key of 1 to 200 characters")
            cached = store.get("requests",idem)
            if cached:
                if cached["fingerprint"] != fingerprint:
                    raise HTTPException(409,"Idempotency key was used for a different request")
                if cached.get("response") is not None:
                    return cached["response"]
                raise HTTPException(409,"This operator request was already attempted. Check the sessions list before starting another task.")
        prompt = body.prompt
        operator_route = "slack-operator" if body.integration == "slack-huddles" else "codex"
        personality = store.get("routes", operator_route, {}).get("speech_engine", {}).get("personality", "")
        if personality:
            prompt = "Operator personality:\n" + personality + "\n\nTask:\n" + prompt
        if body.instructions:
            prompt = "Instructions for this operator:\n"+body.instructions+"\n\nTask:\n"+prompt
        if body.integration == "slack-huddles":
            enabled("slack-huddles")
            task_host = body.host or store.get("settings","main")["default_host"]
            cli = os.environ.get("PHONE_PLATFORM_WORKSTATION_CLI", "/home/raygen/Projects/cisco-phone-shenanigans/phone-platform/bin/switchboard") if task_host == "workstation" else "/opt/phone-platform/current/bin/switchboard"
            prompt = f"You are the owner's Slack phone operator. Use the Switchboard CLI at {cli}. Commands: huddle lookup QUERY; uuid (generate one durable request ID); huddle dial USER_ID --request-id REQUEST_ID; huddle status REQUEST_ID; huddle cancel REQUEST_ID. It reads private credentials itself; never print credentials. Resolve ambiguous names by asking the owner. Start a huddle only when the owner has requested it. Record request IDs and never repeat dial after an uncertain result; check status instead.\n\n"+prompt
        call_id = str(body.call_id) if body.call_id else None
        if call_id:
            if body.integration != "slack-huddles":
                raise HTTPException(400,"Live call handoff is supported by the Slack huddle integration")
            previous = store.get("operator-calls",call_id)
            if previous:
                raise HTTPException(409,"This phone call already has an operator or has ended")
            store.put("operator-calls",call_id,{"call_id":call_id,"state":"active","session_id":None,"created":time.time()})
            prompt = ("You are speaking with the owner on a live phone call. Keep replies brief and conversational. "
                "Use phone_ask_user for clarification so the caller can answer without another call. "
                "Never use ring mode during this call. After resolving the requested person, call the CLI with "
                f"huddle dial USER_ID --request-id REQUEST_ID --mode operator --operator-call-id {call_id}. "
                "This hands the current handset into the huddle; do not place a second dial. "
                "If the caller ends the call, stop. Do not start unrelated background tasks.\n\n"+prompt)
        # Record the claim before a remote side effect. A disconnected client,
        # process restart or lost bridge reply must not silently create a second
        # session when the caller repeats the same idempotency key.
        if idem:
            store.put("requests",idem,{"fingerprint":fingerprint,"state":"attempted","created":time.time()})
        try:
            record = await codex.create_operator(body.title,prompt,body.host or store.get("settings","main")["default_host"],body.cwd,engine=engine)
        except BaseException:
            if idem:
                store.put("requests",idem,{"fingerprint":fingerprint,"state":"unconfirmed","created":time.time()})
            if call_id:
                call = store.get("operator-calls",call_id)
                store.put("operator-calls",call_id,{**call,"state":"closed"})
            raise
        id = "codex:"+str(record["id"])
        if call_id:
            call = store.get("operator-calls",call_id)
            store.put("operator-calls",call_id,{**call,"session_id":id})
            if call["state"] != "active":
                await codex.cancel(id)
                raise HTTPException(409,"The phone call ended before its operator was ready")
        integration = engine if body.integration in {"amp", "codex"} else body.integration
        store.put("session-meta",id,{"integration":integration})
        result = normalize(record)
        if idem:
            store.put("requests",idem,{"fingerprint":fingerprint,"state":"completed","response":result})
        store.event("operator.created",body.title,id)
        return result

    async def start_operator(body, request):
        # Operator creation is quick (it enqueues work). Serialize this boundary
        # to make the durable check/claim atomic across concurrent HTTP callers.
        async with operator_lock:
            return await start_operator_locked(body, request)

    @app.post(API+"/operators",status_code=201)
    async def operator(body: Operator, request: Request):
        return await start_operator(body,request)

    @app.post(API+"/integrations/slack-huddles/operator",status_code=201)
    async def slack_operator(body: Operator, request: Request):
        return await start_operator(body.model_copy(update={"integration":"slack-huddles"}),request)

    @app.get(API+"/operators/calls/{call_id}")
    async def operator_call(call_id: uuid.UUID):
        call = store.get("operator-calls",str(call_id))
        if not call:
            raise HTTPException(404,"Unknown operator phone call")
        handoff = store.get("operator-handoffs",str(call_id),{})
        return {**call, **handoff}

    @app.delete(API+"/operators/calls/{call_id}")
    async def close_operator_call(call_id: uuid.UUID):
        key = str(call_id)
        # Persist a tombstone even if task creation has not arrived yet. A slow
        # HTTP submission must not launch work after the handset disconnected.
        call = store.get("operator-calls",key,{"call_id":key,"session_id":None,"created":time.time()})
        store.put("operator-calls",key,{**call,"state":"closed"})
        cleanup = []
        if call.get("session_id"):
            cleanup.append(codex.cancel(call["session_id"]))
        handoff = store.get("operator-handoffs",key)
        if handoff:
            cleanup.append(huddle().command("cancel",request_id=handoff["request_id"]))
        results = await asyncio.gather(*cleanup,return_exceptions=True)
        if any(isinstance(result,Exception) for result in results):
            raise ConnectorError("The call is closed, but a backend could not confirm cancellation",503)
        return {"closed":True,"call_id":key}

    @app.get(API+"/sessions/{id}")
    async def session(id: str):
        result = normalize(await codex.get_session(id))
        result["inbox"] = await codex.inbox(id,False)
        return result

    @app.post(API+"/sessions/{id}/input")
    async def session_input(id: str, body: Text):
        session = await codex.get_session(id)
        enabled(session.get("engine") or "codex")
        result = await codex.send_input(id,body.text)
        store.event("session.input","Instruction sent",id)
        return result

    @app.post(API+"/sessions/{id}/cancel")
    async def cancel(id: str):
        result = await codex.cancel(id)
        store.event("session.cancelled","Cancellation requested",id)
        return result

    @app.patch(API+"/sessions/{id}")
    async def publish(id: str, body: Update):
        result = await codex.update_session(id,body.summary,body.state)
        store.event("session.updated",body.summary,id)
        return normalize(result)

    def quiet_settings():
        return store.get("quiet-hours", "main", DEFAULT_QUIET)

    async def dispatch_callbacks():
        async with callback_lock:
            if is_quiet(quiet_settings()):
                return
            for item in store.all("callback-queue"):
                if item["state"] != "queued":
                    continue
                # Persist before crossing the bridge. Never retry uncertain calls.
                item.update(state="attempted", updated=time.time())
                store.put("callback-queue", item["id"], item)
                try:
                    if item.get("question"):
                        result = await codex.ask_question(item["session_id"], [{"id":"answer", "question":item["question"], "header":"Question", "options":[{"label":label,"description":""} for label in item["options"]]}], "switchboard:"+item["id"])
                    else:
                        result = await codex.request_callback(item["session_id"])
                    item.update(state="sent", result=result)
                except ConnectorError as error:
                    if error.status_code == 409 and any(reason in str(error).lower() for reason in ("handset is busy", "quiet hours")):
                        item.update(state="queued", detail=str(error))
                    else:
                        item.update(state="unconfirmed", detail=str(error))
                finally:
                    store.put("callback-queue", item["id"], item)
                store.event("callback."+item["state"], "Callback "+item["state"], item["session_id"])
                # One request per monitor pass avoids a burst at quiet-hours end.
                break

    app.state.dispatch_callbacks = dispatch_callbacks

    @app.get(API+"/quiet-hours")
    async def quiet_hours():
        settings = quiet_settings()
        return {**settings, "active":is_quiet(settings)}

    @app.put(API+"/quiet-hours")
    async def save_quiet_hours(body: QuietHours):
        try:
            ZoneInfo(body.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise HTTPException(422, "Choose a valid IANA time zone")
        if body.start == body.end:
            raise HTTPException(422, "Start and end times must differ")
        store.put("quiet-hours", "main", body.model_dump())
        return await quiet_hours()

    @app.get(API+"/callbacks")
    async def callbacks():
        return {"callbacks":sorted(store.all("callback-queue"), key=lambda x:x["created"], reverse=True)}

    @app.delete(API+"/callbacks/{id}")
    async def cancel_callback(id: str):
        async with callback_lock:
            item = store.get("callback-queue", id)
            if not item:
                raise HTTPException(404, "Unknown callback")
            if item["state"] != "queued":
                raise HTTPException(409, "Only a queued callback can be cancelled")
            item["state"] = "cancelled"
            return store.put("callback-queue", id, item)

    @app.post(API+"/sessions/{id}/callback")
    async def callback(id: str, body: Callback, request: Request):
        session = await codex.get_session(id)
        id = "codex:"+str(session["id"])
        key = request.headers.get("idempotency-key") or str(uuid.uuid4())
        if not key.strip() or len(key) > 200:
            raise HTTPException(400, "Use an idempotency key of 1 to 200 characters")
        payload = {"session_id":id, "question":body.question or body.message, "options":body.options}
        async with callback_lock:
            item = store.get("callback-queue", key)
            if item and any(item[k] != v for k,v in payload.items()):
                raise HTTPException(409, "Idempotency key was used for another callback")
            if not item:
                item = {**payload, "id":key, "title":session["title"], "state":"queued", "created":time.time()}
                store.put("callback-queue", key, item)
        await dispatch_callbacks()
        saved = store.get("callback-queue", key)
        return saved.get("result") or saved

    @app.get(API+"/attention")
    async def attention():
        current = await sessions()
        questions = await codex.request("questions")
        pending = [q for q in questions if q.get("state") not in {"answered","cancelled","expired","closed"}]
        items = []
        for session in current:
            matching = [q for q in pending if str(q["session_id"]) == session["id"].removeprefix("codex:")]
            if matching or session["state"] in {"waiting", "error", "interrupted"}:
                items.append({**session, "questions":matching})
        return {"items":items, "quiet_hours":await quiet_hours()}

    @app.get(API+"/task-templates")
    async def templates():
        return {"templates":store.all("task-templates")}

    @app.put(API+"/task-templates/{number}")
    async def save_template(number: str, body: TaskTemplate):
        if number != body.number or body.call_id:
            raise HTTPException(422, "Use a matching speed-dial number without a live call ID")
        if any(r["extension"] == number for r in store.all("routes")):
            raise HTTPException(409, "This number is already a phone route")
        return store.put("task-templates", number, body.model_dump())

    @app.post(API+"/task-templates", status_code=201)
    async def create_template(body: TaskTemplate):
        if store.get("task-templates", body.number):
            raise HTTPException(409, "This speed-dial number already has a saved task")
        return await save_template(body.number, body)

    @app.delete(API+"/task-templates/{number}")
    async def delete_template(number: str):
        store.delete("task-templates", number)
        return {"deleted":True}

    @app.post(API+"/task-templates/{number}/run", status_code=201)
    async def run_template(number: str, request: Request):
        item = store.get("task-templates", number)
        if not item or not item["enabled"]:
            raise HTTPException(404, "Task unavailable")
        if not request.headers.get("idempotency-key"):
            raise HTTPException(400, "A task launch requires an Idempotency-Key")
        body = Operator(**{k:v for k,v in item.items() if k not in {"number","enabled"}})
        return await start_operator(body, request)

    @app.post(API+"/questions/{id}/answer")
    async def answer(id: str, body: Answer):
        question = await codex.request("question",question_id=id,wait_seconds=0)
        result = await codex.answer_question(question["session_id"],id,body.text,body.item_id)
        store.event("question.answered","Answer delivered","codex:"+str(question["session_id"]))
        return result

    @app.get(API+"/questions/{id}")
    async def question(id: str, wait_seconds: int=0):
        return await codex.request("question",question_id=id,wait_seconds=max(0,min(wait_seconds,120)))

    @app.post(API+"/phone/rpc")
    async def rpc(body: dict):
        allowed = {"sessions","session","capabilities","create","select_host","submit","interrupt","register","update","ask","answer","question","questions","inbox","ring","amp_poll","amp_ack"}
        if body.get("method") not in allowed or not isinstance(body.get("params",{}),dict):
            raise HTTPException(400,"Unknown phone method")
        result = await codex.request(body["method"],**body.get("params",{}))
        if body["method"] not in {"sessions","session","capabilities","questions","question","inbox","amp_poll"}:
            store.event("phone."+body["method"],"Phone integration request")
        return {"result":result}

    @app.get(API+"/integrations")
    async def list_integrations():
        return {"integrations":await integrations()}

    @app.put(API+"/integrations/{id}")
    async def update_integration(id: str, body: IntegrationUpdate):
        record = store.get("integrations",id)
        if not record:
            raise HTTPException(404,"Unknown integration")
        changes = body.config or {}
        allowed = {ENGINE_PROVIDER:{"engine_id","upstream_url","voice_id","model_id","language"},"whisper":{"model","language"},"piper":{"voice"},"elevenlabs-stt":{"model_id","language"},"elevenlabs-tts":{"model_id","voice_id"},"amp":{"host"},"codex":{"host"}}
        if set(changes)-allowed.get(id,set()):
            raise HTTPException(400,"Unsupported configuration field")
        if id == ENGINE_PROVIDER:
            validate_engine_config(changes)
        elif any(not isinstance(value,str) or len(value)>100 or not re.fullmatch(r"[\w.\-]*",value) for value in changes.values()):
            raise HTTPException(400,"Invalid provider configuration")
        if id == "whisper" and changes.get("model", "base.en") not in {"tiny","tiny.en","base","base.en","small","small.en","medium","medium.en","large-v3","large-v3-turbo"}:
            raise HTTPException(400,"Unsupported Whisper model")
        if id == "piper" and changes.get("voice", "en_US-lessac-medium") != "en_US-lessac-medium":
            raise HTTPException(400,"Only the installed Piper voice is available")
        if id in {"amp", "codex"} and changes.get("host", "proxmox") not in {"proxmox","workstation"}:
            raise HTTPException(400,"Choose Proxmox or workstation")
        record["config"].update(changes)
        if body.enabled is not None:
            record["enabled"] = body.enabled
        if body.api_key is not None:
            if id not in {ENGINE_PROVIDER,"elevenlabs-stt","elevenlabs-tts","slack-huddles"}:
                raise HTTPException(400,"This integration uses its existing host credentials")
            store.set_credential(id,body.api_key.strip())
        store.put("integrations",id,record)
        health_cache["time"] = 0
        store.event("integration.configured",record["name"]+" configuration saved")
        return next(item for item in await integrations() if item["id"] == id)

    @app.post(API+"/integrations/slack-huddles/control")
    async def huddle_control(body: dict):
        enabled("slack-huddles")
        command = body.get("command")
        args = {k:v for k,v in body.items() if k!="command"}
        client = huddle()
        if command == "lookup":
            result = await client.lookup(args.get("query", ""))
            for user in result.get("users",[]):
                store.put("slack-users",user["id"],user)
        elif command == "recent":
            result = await client.recent()
        elif command == "dial":
            async with huddle_dial_lock:
                call_id = str(uuid.UUID(args["operator_call_id"])) if args.get("operator_call_id") else None
                if call_id:
                    call = store.get("operator-calls",call_id)
                    if not call or call["state"] != "active" or args.get("mode") != "operator":
                        raise HTTPException(409,"This operator phone call is not active")
                    existing = store.get("operator-handoffs",call_id)
                    if existing and existing["request_id"] != args.get("request_id"):
                        raise HTTPException(409,"This operator call already has a huddle handoff")
                result = await client.dial(args.get("user_id", ""),args.get("request_id"),args.get("mode","ring"))
                if call_id:
                    store.put("operator-handoffs",call_id,{"request_id":result["request_id"],"user_id":args["user_id"]})
                    if store.get("operator-calls",call_id)["state"] != "active":
                        await client.command("cancel",request_id=result["request_id"])
                        raise HTTPException(409,"The phone call ended before its huddle was ready")
                store.put("pending-huddles",result["request_id"],{"request_id":result["request_id"],"user_id":args["user_id"],"created":time.time()})
                store.event("huddle.requested","Huddle requested")
        elif command in {"status","cancel"}:
            result = await client.command(command,request_id=args.get("request_id"))
            if command == "status" and result.get("phase") == "connected":
                user_id, connected_at = result.get("user_id"), result.get("connected_at")
                if (isinstance(user_id,str) and re.fullmatch(r"[UW][A-Z0-9]+",user_id)
                        and isinstance(connected_at,(int,float)) and math.isfinite(connected_at) and connected_at > 0):
                    contact = store.get("slack-users",user_id,{})
                    name = result.get("display_name") or contact.get("name") or user_id
                    # Assign the same stable callback number as Recent Slack
                    # huddles, only after the handset has actually connected.
                    saved = next((c for c in store.contacts() if c["user_id"] == user_id),None)
                    if not saved or saved["updated"] < connected_at:
                        store.contact(user_id,name,contact.get("username", ""),connected_at)
                        saved = next((c for c in store.contacts() if c["user_id"] == user_id),None)
                    result = {**result,"display":{"name":name,"number":f'88{saved["id"]:04d}' if saved else user_id}}
        else:
            raise HTTPException(400,"Unknown huddle command")
        return result

    @app.post(API+"/integrations/home-assistant/conversation")
    async def home_assistant_conversation(body: Conversation):
        enabled("home-assistant")
        private = read_private(config.home_assistant_config)
        token, url = private.get("token", ""), private.get("url", "").rstrip("/")
        parsed = urlsplit(url)
        if not token or parsed.scheme not in {"http","https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise HTTPException(503,"Configure the existing Home Assistant phone connection first")
        payload = {"text":body.text,"language":"en"}
        if body.conversation_id:
            payload["conversation_id"] = body.conversation_id
        if private.get("agent_id"):
            payload["agent_id"] = private["agent_id"]
        try:
            async with httpx.AsyncClient(timeout=120,trust_env=False,follow_redirects=False) as client:
                response = await client.post(url+"/api/conversation/process",json=payload,headers={"Authorization":"Bearer "+token})
                response.raise_for_status()
                result = response.json()
                if not isinstance(result,dict):
                    raise ValueError("Invalid conversation response")
                return result
        except httpx.TimeoutException:
            raise ConnectorError("Home Assistant timed out; check device state before repeating a command",504) from None
        except (httpx.HTTPError,ValueError):
            raise ConnectorError("Home Assistant could not process the conversation; check its connection and credentials",503) from None

    @app.get(API+"/settings")
    async def settings():
        return store.get("settings","main")

    @app.patch(API+"/settings")
    async def save_settings(body: Settings):
        result = store.get("settings","main")
        result.update(body.model_dump(exclude_none=True))
        for kind in ("stt","tts"):
            enabled(result["default_"+kind])
        store.put("settings","main",result)
        store.event("settings.updated","Defaults saved")
        return result

    @app.get(API+"/routes")
    async def routes():
        return {"routes":store.all("routes")}

    async def route_update_locked(id: str, body: Route):
        old = store.get("routes",id)
        if not old:
            raise HTTPException(404,"Unknown route")
        compatibility = {}
        if id == "codex" and body.integration == "codex":
            compatibility["integration"] = "amp"
        if "speech_mode" not in body.model_fields_set:
            compatibility["speech_mode"] = old.get("speech_mode", "legacy")
        if "speech_engine" not in body.model_fields_set:
            compatibility["speech_engine"] = OperatorProfile.model_validate(old.get("speech_engine") or {})
        body = body.model_copy(update=compatibility)
        native = next(r for r in ROUTES if r["id"]==id)
        if body.integration != native["integration"]:
            raise HTTPException(400,"Choose speech providers or a dial alias for this integration")
        ext = body.extension
        if ext != native["extension"] and (ext in {"0","555","600","601","602","611","911","112","999"} or ext.startswith(("6","88","89"))):
            raise HTTPException(409,"This extension is reserved by the phone system")
        if any(r["extension"]==ext and r["id"]!=id for r in store.all("routes")):
            raise HTTPException(409,"This extension is already assigned")
        if ext != old["extension"] and not config.route_reload:
            raise HTTPException(409,"Dial aliases require the Asterisk route include to be provisioned")
        record = {**body.model_dump(),"id":id}
        if body.speech_mode == "speech-engine":
            if id not in {"codex", "slack-operator"}:
                raise HTTPException(400,"Continuous Speech Engine supports Amp and Slack operators")
            if not body.speech_engine.engine_id:
                raise HTTPException(400,"Save settings and provision this operator's Speech Engine before selecting it")
        engine_id = body.speech_engine.engine_id
        if engine_id and any(r["id"] != id and r.get("speech_engine", {}).get("engine_id") == engine_id for r in store.all("routes")):
            raise HTTPException(409,"Each operator must use its own Speech Engine resource")
        records = [record if r["id"]==id else r for r in store.all("routes")]
        if config.route_reload and (ext != old["extension"] or body.enabled != old["enabled"]):
            directory = config.state_dir / "asterisk"
            directory.mkdir(mode=0o700,exist_ok=True)
            contents = "[switchboard-routes]\n"
            for r in records:
                target = next(n["extension"] for n in ROUTES if n["id"]==r["id"])
                if r["enabled"] and r["extension"] != target:
                    contents += f'exten => {r["extension"]},1,Goto(sccp-internal,{target},1)\n'
            path = directory / "routes.conf"
            previous = path.read_text() if path.exists() else "[switchboard-routes]\n"
            def write(value):
                temp = directory / "routes.tmp"
                temp.write_text(value)
                temp.chmod(0o644)
                temp.replace(path)
            write(contents)
            process = None
            try:
                process = await asyncio.create_subprocess_exec("docker","exec","sccp-pbx","asterisk","-rx","dialplan reload",stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                output, _ = await asyncio.wait_for(process.communicate(),15)
                if process.returncode or b"failed" in output.lower():
                    raise HTTPException(503,"Asterisk did not reload the dial aliases")
            except BaseException as error:
                write(previous)
                if process is not None and process.returncode is None:
                    process.kill()
                    await process.wait()
                if isinstance(error, (OSError, TimeoutError)):
                    raise HTTPException(503,"Asterisk could not reload the dial aliases; the previous configuration was restored") from None
                raise
        store.put("routes",id,record)
        store.event("route.updated",record["name"]+" route saved")
        return record

    @app.put(API+"/routes/{id}")
    async def route_update(id: str, body: Route):
        async with route_lock:
            return await route_update_locked(id, body)

    @app.post(API+"/speech/transcribe")
    async def transcribe(file: UploadFile=File(...), provider: str|None=Form(None), language: str|None=Form(None), route: str|None=Form(None)):
        audio = await file.read(MAX_AUDIO+1)
        await file.close()
        if not audio or len(audio)>MAX_AUDIO:
            raise HTTPException(413,"Upload an audio file smaller than 25 MiB")
        return await speech.transcribe(audio,Path(file.filename or "audio.wav").name,provider,language,route)

    @app.post(API+"/speech/engine/provision")
    async def provision_speech_engine():
        result = await engine.provision()
        health_cache["time"] = 0
        store.event("speech_engine.configured", "Speech Engine resource configured")
        return result

    @app.post(API+"/speech/engine/provision/{route}")
    async def provision_operator_engine(route: str):
        if route not in {"codex", "slack-operator"}:
            raise HTTPException(400,"Choose Amp or Slack operator")
        result = await engine.provision(route)
        health_cache["time"] = 0
        store.event("speech_engine.configured", route + " Speech Engine configured")
        return result

    @app.get(API+"/speech/engine/voices")
    async def engine_voices():
        async with engine.provider_client() as client:
            result = await client.voices.get_all(request_options={"max_retries": 0})
        return {"voices": [{"id": v.voice_id, "name": v.name} for v in result.voices]}

    @app.post(API+"/speech/engine/preview/{route}")
    async def engine_preview(route: str, body: Text):
        if route not in {"codex", "slack-operator"}:
            raise HTTPException(400,"Choose Amp or Slack operator")
        if len(body.text) > 1000:
            raise HTTPException(400,"Voice previews are limited to 1000 characters")
        settings, _ = engine.settings(require_engine=False, route=route)
        if not settings.get("voice_id"):
            raise HTTPException(400,"Choose a voice first")
        profile = store.get("routes", route, {}).get("speech_engine", {})
        options = {k: profile[k] for k in ("speed", "stability", "similarity_boost") if profile.get(k) is not None}
        audio = bytearray()
        async with engine.provider_client() as client:
            async for chunk in client.text_to_speech.convert(voice_id=settings["voice_id"], text=body.text,
                    model_id=settings["model_id"], output_format="pcm_16000", voice_settings=options,
                    request_options={"max_retries": 0}):
                audio.extend(chunk)
                if len(audio) > 8 * 1024 * 1024:
                    raise SpeechError("Voice preview exceeded its audio limit")
        return Response(pcm_wav(bytes(audio), 16000), media_type="audio/wav")

    @app.websocket("/speech-engine/upstream")
    async def speech_engine_upstream(websocket: WebSocket):
        # WebSockets use the provider's signed token, not dashboard HTTP auth.
        await engine.upstream(websocket)

    @app.post(API+"/speech/synthesize")
    async def synthesize(body: Synthesis):
        audio, mime = await speech.synthesize(body.text,body.provider,body.route)
        return Response(audio,media_type=mime)

    @app.post(API+"/speech/phone/transcribe")
    async def phone_transcribe(body: PhoneAudio):
        try:
            pcm = base64.b64decode(body.audio,validate=True)
        except ValueError:
            raise HTTPException(400,"Invalid PCM audio")
        if not pcm or len(pcm)%2 or len(pcm)>8000*2*120:
            raise HTTPException(400,"Provide at most two minutes of signed 8kHz mono PCM")
        return await speech.transcribe(pcm_wav(pcm),route=body.route,prompt=body.prompt)

    @app.post(API+"/speech/phone/synthesize")
    async def phone_synthesize(body: Synthesis):
        audio, _ = await speech.synthesize(body.text,body.provider,body.route)
        return Response(await asyncio.to_thread(telephone_wav,audio),media_type="audio/wav")

    @app.get(API+"/favorites")
    async def favorites():
        return store.get("favorites", "main", {"entries":[], "revision":0})

    @app.put(API+"/favorites")
    async def save_favorites(body: Favorites):
        current = await favorites()
        if body.revision != current["revision"]:
            raise HTTPException(409, "Favorites changed in another window. Refresh before saving again.")
        entries = [entry.model_dump() for entry in body.entries]
        if len(entries) > 50:
            raise HTTPException(422, "Keep up to 50 favorite people")
        if len({entry["id"] for entry in entries}) != len(entries):
            raise HTTPException(422, "Favorite IDs must be unique")
        if len({entry["number"] for entry in entries}) != len(entries):
            raise HTTPException(409, "This number is already in your favorites")
        result = store.put("favorites", "main", {"entries":entries, "revision":current["revision"]+1})
        store.event("favorites.updated", "Favorite people updated")
        return result

    async def directories():
        records = store.all("directories")
        favorite_entries = (await favorites())["entries"]
        records.insert(0, {"id":"favorite-people", "name":"Favorite people", "integration":"custom", "enabled":True, "builtin":True, "entries":favorite_entries})
        tasks = store.all("task-templates")
        if tasks:
            records.append({"id":"speed-dial-tasks", "name":"Saved tasks", "integration":"amp", "enabled":True, "builtin":True,
                "entries":[{"id":t["number"],"name":t["title"],"description":"Start a saved task","number":t["number"]} for t in tasks if t["enabled"]]})
        current = None
        for record in records:
            if record["id"] in {"amp", "codex"}:
                try:
                    if current is None:
                        current = await sessions()
                    record["entries"] = [{"id":s["id"],"name":s["title"],"description":s["state"]+" · "+(s.get("executor") or s.get("host","proxmox")),"number":s["extension"],"updated":s["updated"]} for s in reversed(current) if s["engine"] == record["id"] and (s.get("kind") == "managed" or s["integration"] == record["id"])]
                    record["status"] = "ready"
                except ConnectorError:
                    record["entries"],record["status"] = [],"offline"
            elif record["id"] == "slack-recent":
                record["entries"] = [{"id":str(c["id"]),"name":c["name"],"description":c["username"],"number":f'88{c["id"]:04d}',"updated":c["updated"]} for c in store.contacts()]
        return records

    @app.get(API+"/directories")
    async def directory_list():
        return {"directories":await directories()}

    @app.post(API+"/directories",status_code=201)
    async def directory_create(body: Directory):
        if body.id in {"favorite-people", "speed-dial-tasks"} or store.get("directories",body.id):
            raise HTTPException(409,"This directory is already registered")
        record = {**body.model_dump(),"entries":[],"builtin":False}
        store.put("directories",body.id,record)
        store.event("directory.registered",body.name)
        return record

    @app.put(API+"/directories/{id}")
    async def directory_update(id: str, body: DirectoryUpdate):
        record = store.get("directories",id)
        if not record:
            raise HTTPException(404,"Unknown directory")
        record.update(body.model_dump(exclude_none=True))
        return store.put("directories",id,record)

    @app.put(API+"/directories/{id}/entries")
    async def directory_entries(id: str, body: Entries):
        record = store.get("directories",id)
        if not record:
            raise HTTPException(404,"Unknown directory")
        if record.get("builtin"):
            raise HTTPException(409,"This directory is maintained by its integration")
        entries = [e.model_dump() for e in body.entries]
        if len({e["id"] for e in entries}) != len(entries):
            raise HTTPException(400,"Directory entry IDs must be unique")
        record["entries"] = entries
        return store.put("directories",id,record)

    @app.delete(API+"/directories/{id}")
    async def directory_delete(id: str):
        record = store.get("directories",id)
        if not record:
            raise HTTPException(404,"Unknown directory")
        if record.get("builtin"):
            raise HTTPException(409,"Disable built-in directories instead of removing them")
        store.delete("directories",id)
        return {"deleted":True}

    @app.post(API+"/directories/resolve")
    async def directory_resolve(body: dict):
        directory = store.get("directories","slack-recent")
        if not directory or not directory["enabled"]:
            raise HTTPException(404,"Recent huddle directory is disabled")
        number = body.get("number", "")
        if not isinstance(number,str) or not re.fullmatch(r"88\d{4}",number):
            raise HTTPException(400,"Unknown directory dial target")
        contact = next((c for c in store.contacts() if c["id"]==int(number[2:])),None)
        if not contact:
            raise HTTPException(404,"This recent huddle contact is no longer available")
        return {"integration":"slack-huddles","target":{"user_id":contact["user_id"],"name":contact["name"],"username":contact["username"]}}

    @app.get("/phone/directories",include_in_schema=False)
    async def phone_directories(request: Request, token: str="", directory: str|None=None, offset: int=0):
        if not secrets.compare_digest(token.encode(),store.phone_token.encode()):
            raise HTTPException(401,"Phone directory credential required")
        offset = max(0,offset)
        available = [d for d in await directories() if d["enabled"]]
        base = str(request.base_url).rstrip("/")+"/phone/directories?"
        if directory is None:
            root = ET.Element("CiscoIPPhoneMenu")
            ET.SubElement(root,"Title").text = "Switchboard directories"
            ET.SubElement(root,"Prompt").text = "Choose an application"
            for d in available:
                item = ET.SubElement(root,"MenuItem")
                ET.SubElement(item,"Name").text = d["name"][:64]
                ET.SubElement(item,"URL").text = base+urlencode({"token":token,"directory":d["id"]})
        else:
            record = next((d for d in available if d["id"]==directory),None)
            if not record:
                raise HTTPException(404,"Directory unavailable")
            root = ET.Element("CiscoIPPhoneDirectory")
            ET.SubElement(root,"Title").text = record["name"][:64]
            entries = record.get("entries",[])
            ET.SubElement(root,"Prompt").text = "Select a number to dial" if entries else "No entries yet"
            for entry in entries[offset:offset+25]:
                item = ET.SubElement(root,"DirectoryEntry")
                ET.SubElement(item,"Name").text = entry["name"][:64]
                ET.SubElement(item,"Telephone").text = entry["number"]
            keys = [("Dial","SoftKey:Dial",1)]
            if offset+25<len(entries):
                keys.append(("Next",base+urlencode({"token":token,"directory":directory,"offset":offset+25}),2))
            if offset:
                keys.append(("Previous",base+urlencode({"token":token,"directory":directory,"offset":max(0,offset-25)}),3))
            keys.append(("Back",base+urlencode({"token":token}),4))
            for name,url,position in keys:
                key = ET.SubElement(root,"SoftKeyItem")
                for tag,value in [("Name",name),("URL",url),("Position",str(position))]:
                    ET.SubElement(key,tag).text = value
        return Response(ET.tostring(root,encoding="utf-8",xml_declaration=True),media_type="text/xml")

    @app.get(API+"/events")
    async def events(after: int=0, limit: int=100):
        return {"events":store.events(max(0,after),max(1,min(limit,500)))}

    @app.get(API+"/events/stream")
    async def event_stream(request: Request, after: int=0):
        async def generate():
            cursor = after
            while not await request.is_disconnected():
                for item in store.events(cursor,500):
                    cursor = item["id"]
                    yield f'id: {cursor}\nevent: update\ndata: {json.dumps(item)}\n\n'
                yield ": keepalive\n\n"
                await asyncio.sleep(2)
        return StreamingResponse(generate(),media_type="text/event-stream",headers={"X-Accel-Buffering":"no"})

    @app.get("/api/docs",include_in_schema=False)
    async def docs():
        routes = app.openapi()["paths"]
        rows = "".join("<tr><td>"+html.escape(method.upper())+"</td><td><code>"+html.escape(path)+"</code></td><td>"+html.escape(spec.get("summary",""))+"</td></tr>" for path,methods in routes.items() for method,spec in methods.items())
        examples = [
            ("Create a reusable operator", "POST /api/v1/operators", {"title":"My helper","prompt":"Prepare a status update","integration":"my-app","instructions":"Ask the owner when a detail is missing"}),
            ("Register an existing session for phone callbacks", "POST /api/v1/sessions", {"title":"Build task","integration":"my-app","external_key":"stable-thread-id"}),
            ("Send input to a running session", "POST /api/v1/sessions/codex:1/input", {"text":"Use the office lights"}),
            ("Queue a clarification call", "POST /api/v1/sessions/codex:1/callback", {"question":"Which room?","options":["Office","Kitchen"]}),
            ("Publish an optional application directory", "POST /api/v1/directories", {"id":"my-app","name":"My application","integration":"my-app"}),
            ("Replace a directory's entries", "PUT /api/v1/directories/my-app/entries", {"entries":[{"id":"example","name":"Example destination","number":"123"}]}),
        ]
        examples_html = "".join("<h3>"+html.escape(title)+"</h3><pre>"+html.escape(method)+"\n"+html.escape(json.dumps(body,indent=2))+"</pre>" for title,method,body in examples)
        return HTMLResponse('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Switchboard API</title><style>body{font:16px/1.6 system-ui,sans-serif;max-width:1000px;margin:40px auto;padding:0 24px;background:#121617;color:#e4ece8}a{color:#9cdbb3}code,pre{background:#1c2424}pre{padding:16px;overflow:auto;border-radius:8px}table{border-collapse:collapse;width:100%;font-size:14px}td,th{text-align:left;padding:9px;border-bottom:1px solid #34413b}h2{margin-top:40px}</style></head><body><h1>Switchboard API</h1><p>Authenticate with <code>Authorization: Bearer TOKEN</code>. Read the token from the private token file; keep it out of prompts and source code. The dashboard uses an HttpOnly session cookie.</p><p><a href="/api/openapi.json">Download OpenAPI schema</a> · <a href="/">Dashboard</a></p><h2>Build an integration</h2><p>New operators use Amp. Sessions keep their compatible <code>codex:1</code> IDs and phone extensions; inspect <code>engine</code>, <code>executor</code> and <code>thread_url</code> to identify and review their execution. Creating a session does not ring the handset. Use <code>Idempotency-Key</code> on operator creation and reuse that key for the same request. An uncertain response must be checked against the session list before starting another task.</p>'+examples_html+'<p>Directory registration is optional. Apps without a directory still use operators, callbacks and speech. Built-in Amp, Codex and recent Slack directories are maintained by their integrations.</p><h2>Speech and events</h2><p>Send multipart <code>file</code>, optional <code>provider</code> and <code>route</code> fields to <code>POST /api/v1/speech/transcribe</code>. Send JSON <code>{"text":"Hello","route":"codex"}</code> to <code>POST /api/v1/speech/synthesize</code>. Phone adapters use the corresponding <code>/speech/phone/</code> endpoints for 8 kHz mono audio. Audio is processed without saving uploads.</p><p>Poll <code>GET /api/v1/events?after=EVENT_ID</code> or consume the authenticated server-sent event stream at <code>/api/v1/events/stream</code>. The phone directory credential only reads Cisco XML; it cannot access this API.</p><h2>Endpoint reference</h2><table><tr><th>Method</th><th>Path</th><th>Operation</th></tr>'+rows+'</table></body></html>')

    if config.web_dir.exists():
        app.mount("/web",StaticFiles(directory=config.web_dir),name="web")

    @app.get("/",include_in_schema=False)
    async def index():
        return FileResponse(config.web_dir / "index.html")

    return app

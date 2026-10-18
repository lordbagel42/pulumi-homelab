"""Authenticated server-to-server PCM calls. No cloud speech or recording."""

import asyncio
import contextlib
import fcntl
import logging
import re
import secrets
import sqlite3
import struct
import time
import uuid
from typing import Annotated

from anyio import CancelScope
from fastapi import APIRouter, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel, ConfigDict, Field

from .attention import DEFAULT_QUIET, is_quiet
from .call_pbx import PhoneBusy, PhonePBX
from .incoming import IncomingGate

LOG = logging.getLogger("switchboard.calls")
PREFIX = "/api/v1/calls"
FINAL = {"ended", "failed", "expired", "interrupted"}
AUDIO = {"encoding": "pcm_s16le", "sample_rate": 8000, "channels": 1, "frame_bytes": 320}


class CallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_duration_seconds: int = Field(default=600, ge=1, le=3600, strict=True)


class Stopped(Exception):
    pass


class StreamCall:
    def __init__(self, record):
        self.record = record
        self.media_id = str(uuid.uuid4())
        self.attached = asyncio.Event()
        self.media_claimed = False
        self.control_ready = False
        self.answered = asyncio.Event()
        self.stop = asyncio.Event()
        self.to_phone = asyncio.Queue(maxsize=25)
        self.to_client = asyncio.Queue(maxsize=50)
        self.writer = None
        self.task = None
        self.reason = None

    def end(self, reason):
        if not self.stop.is_set():
            self.reason = reason
            self.stop.set()

    def emit(self, value):
        if self.to_client.full():
            self.end("slow_consumer")
            return
        self.to_client.put_nowait(value)


class Calls:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.token = config.token or store.token_file("call-token")
        self.active = self.server = self.lock_file = None
        self.incoming_server = None
        self.connections = set()

    def authorized(self, header):
        if not header.startswith("Bearer "):
            return False
        token = header[7:].encode()
        return bool(token) and any(
            secret and secrets.compare_digest(token, secret.encode()) for secret in (self.token, self.store.admin_token)
        )

    async def start(self):
        if len(self.token) < 32:
            raise ValueError("The call token must contain at least 32 characters")
        self.lock_file = (self.store.directory / "stream-calls.lock").open("a")
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock_file.close()
            self.lock_file = None
            raise RuntimeError("Calls require a single Switchboard worker") from None
        for record in self.store.all("stream-calls"):
            if record["state"] not in FINAL:
                record.update(state="interrupted", reason="service_restarted", updated=time.time())
                self.store.put("stream-calls", record["id"], record)
        self.server = await asyncio.start_server(self.audio, "127.0.0.1", self.config.audio_port)
        self.incoming_server = await asyncio.start_server(
            self.incoming, "127.0.0.1", self.config.incoming_port, limit=4096
        )

    def describe(self, record):
        return {key: value for key, value in record.items() if key != "request_id"} | {
            "direction": record.get("direction", "outbound"),
            "media_path": f"{PREFIX}/{record['id']}/media",
            "audio": AUDIO,
        }

    def get(self, call_id):
        record = self.store.get("stream-calls", str(call_id))
        if not record:
            raise HTTPException(404, "Call not found")
        return self.describe(record)

    def quiet(self):
        return is_quiet(self.store.get("quiet-hours", "main", DEFAULT_QUIET))

    def create(self, request_id, body):
        try:
            request_id = str(uuid.UUID(request_id))
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(400, "Idempotency-Key must be a UUID") from None
        for record in self.store.all("stream-calls"):
            if record["request_id"] == request_id:
                if record["max_duration_seconds"] != body.max_duration_seconds:
                    raise HTTPException(409, "Idempotency-Key was used for a different request")
                return self.describe(record)
        if not self.server:
            raise HTTPException(503, "Call audio listener is unavailable")
        if not re.fullmatch(r"(?:SCCP|PJSIP|SIP)/[A-Za-z0-9_.-]+", self.config.channel) or not self.config.ami_secret:
            raise HTTPException(503, "Configure the PBX handset channel and AMI credentials")
        if self.active:
            raise HTTPException(409, "A streaming call is already active")
        if self.quiet():
            raise HTTPException(409, "The handset is in quiet hours")
        now = time.time()
        record = {
            "id": str(uuid.uuid4()),
            "request_id": request_id,
            "state": "awaiting_media",
            "reason": None,
            "created": now,
            "updated": now,
            "max_duration_seconds": body.max_duration_seconds,
        }
        self.store.put("stream-calls", record["id"], record)
        call = StreamCall(record)
        self.active = call
        call.task = asyncio.create_task(self.run(call))
        return self.describe(record)

    async def incoming(self, reader, writer):
        task = asyncio.current_task()
        self.connections.add(task)
        gate = IncomingGate(reader, writer)
        try:
            await gate.read_environment(self.config.channel)
            if not self.server or not self.config.ami_secret or self.active:
                return
            request_id = "inbound:" + gate.uniqueid
            if any(record["request_id"] == request_id for record in self.store.all("stream-calls")):
                return  # Never recreate an offer after disconnect/restart.
            now = time.time()
            record = {
                "id": str(uuid.uuid4()),
                "request_id": request_id,
                "direction": "inbound",
                "state": "offered",
                "reason": None,
                "created": now,
                "updated": now,
                "expires_at": now + self.config.offer_seconds,
                "max_duration_seconds": 600,
            }
            self.store.put("stream-calls", record["id"], record)
            call = StreamCall(record)
            self.active = call
            gate.task = asyncio.create_task(gate.watch(call))
            call.task = asyncio.create_task(self.run(call, gate))
            await asyncio.shield(call.task)
        except (OSError, ValueError, TimeoutError, sqlite3.Error):
            LOG.warning("Inbound call admission failed")
        finally:
            await gate.close()
            self.connections.discard(task)

    def inbox(self):
        call = self.active
        return {
            "calls": [self.describe(call.record)]
            if call
            and call.control_ready
            and call.record["state"] == "offered"
            and not call.media_claimed
            and not call.stop.is_set()
            and time.time() < call.record["expires_at"]
            else []
        }

    def state(self, call, state, reason=None):
        call.record.update(state=state, reason=reason, updated=time.time())
        try:
            self.store.put("stream-calls", call.record["id"], call.record)
        except (OSError, sqlite3.Error) as error:
            # Persistence must never block teardown of a real telephone call.
            # Stop before the next PBX action, but retain ownership through hangup.
            call.end("storage_error")
            call.record.update(state="interrupted", reason="storage_error")
            LOG.error("Call state write failed error_type=%s", type(error).__name__)
        call.emit({"type": "state", **{key: call.record[key] for key in ("id", "state", "reason")}})

    async def until(self, call, operation, timeout, pbx=None):
        if call.stop.is_set():
            operation.close()
            raise Stopped()
        task = asyncio.create_task(operation)
        stops = [asyncio.create_task(call.stop.wait())]
        if pbx:
            stops += [asyncio.create_task(pbx.ended.wait()), asyncio.create_task(pbx.failure.wait())]
        try:
            done, _ = await asyncio.wait([task, *stops], timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if any(stop in done for stop in stops):
                raise Stopped()
            if task not in done:
                raise TimeoutError()
            return task.result()
        finally:
            for item in (task, *stops):
                item.cancel()
            await asyncio.gather(task, *stops, return_exceptions=True)

    async def run(self, call, gate=None):
        pbx = PhonePBX(self.config, gate.uniqueid if gate else call.media_id)
        if gate:
            pbx.adopt(gate.channel)
        final = "ended"
        try:
            if call.stop.is_set():
                raise Stopped()
            port = self.server.sockets[0].getsockname()[1]
            if gate:
                # Inbound is an intentional handset action, not an unsolicited
                # ring. Quiet hours apply only to outbound calls.
                await self.until(call, pbx.connect(), 6)
                # Do not offer a call that DELETE would cancel during AMI login,
                # losing the connection needed to confirm exact-channel cleanup.
                call.control_ready = True
                await self.until(call, call.attached.wait(), max(0, call.record["expires_at"] - time.time()), pbx)
                self.state(call, "answering")
                await self.until(call, gate.release(call.media_id, port, call.record["max_duration_seconds"]), 5, pbx)
            else:
                await self.until(call, call.attached.wait(), self.config.attach_seconds)
                if self.quiet():
                    call.end("quiet_hours")
                    raise Stopped()
                self.state(call, "dialing")
                await self.until(call, pbx.connect(), 6)
                await self.until(call, pbx.originate(port, call.record["max_duration_seconds"]), 11)
            await self.until(call, call.answered.wait(), self.config.ring_seconds + 5, pbx)
            await self.until(call, call.stop.wait(), call.record["max_duration_seconds"], pbx)
        except Stopped:
            call.end("pbx_disconnected" if pbx.failure.is_set() else "phone_hangup")
        except PhoneBusy:
            final = "failed"
            call.end("busy")
        except TimeoutError:
            state = call.record["state"]
            final = "expired" if state in {"awaiting_media", "offered"} else "ended"
            call.end(
                "media_timeout"
                if state in {"awaiting_media", "offered"}
                else "time_limit"
                if state == "connected"
                else "answer_timeout"
            )
        except asyncio.CancelledError:
            final = "interrupted"
            call.end("service_stopped")
        except (OSError, ValueError, sqlite3.Error) as error:
            LOG.error("Call failed error_type=%s", type(error).__name__)
            final = "failed"
            call.end("pbx_error")
        finally:
            call.end("ended")
            self.state(call, "ending", call.reason)
            if call.writer:
                call.writer.close()
            try:
                outcome = await pbx.hangup()
                if outcome is None:
                    final = "interrupted"
                    call.reason = "hangup_unconfirmed"
                elif call.reason == "phone_audio_disconnected":
                    # A TCP reset/write failure can race with remote hangup.
                    # Only PBX confirmation that it was gone makes this normal.
                    call.reason = "phone_hangup" if outcome == "ended" else "phone_audio_error"
            finally:
                await pbx.close()
                if gate:
                    await gate.close()
                # Make room for the final event; never retain stale audio on exit.
                while not call.to_client.empty():
                    call.to_client.get_nowait()
                self.state(call, final, call.reason)
                call.to_client.put_nowait(None)
                if self.active is call:
                    self.active = None

    async def cancel(self, call_id):
        self.get(call_id)
        call = self.active
        if call and call.record["id"] == str(call_id):
            call.end("cancelled")
            await asyncio.shield(call.task)
        return self.get(call_id)

    async def websocket(self, ws, call_id):
        call = self.active
        if (
            not self.authorized(ws.headers.get("authorization", ""))
            or ws.headers.get("origin")
            or not call
            or call.record["id"] != str(call_id)
            or call.media_claimed
            or call.stop.is_set()
            or (call.record.get("direction") == "inbound" and not call.control_ready)
            or (call.record["state"] == "offered" and time.time() >= call.record["expires_at"])
        ):
            await ws.close(code=1008)
            return
        # Claim before the first await, so simultaneous sockets cannot both dial.
        call.media_claimed = True
        tasks = []
        try:
            await ws.accept()
            call.attached.set()
            await asyncio.wait_for(ws.send_json({"type": "state", **self.describe(call.record)}), 2)

            async def receive():
                while True:
                    message = await ws.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    data = message.get("bytes")
                    if data is None or len(data) != 320:
                        call.end("invalid_audio")
                        return
                    if not call.answered.is_set():
                        continue  # Never play queued speech from before answer.
                    if call.to_phone.full():
                        call.end("audio_overflow")
                        return
                    call.to_phone.put_nowait(data)

            async def send():
                while True:
                    message = await call.to_client.get()
                    if message is None:
                        return
                    operation = ws.send_bytes(message) if isinstance(message, bytes) else ws.send_json(message)
                    await asyncio.wait_for(operation, 2)

            tasks = [asyncio.create_task(receive()), asyncio.create_task(send())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (WebSocketDisconnect, OSError, TimeoutError):
            pass
        finally:
            call.end("media_disconnected")
            for task in tasks:
                task.cancel()
            # ASGI uses level cancellation: cleanup awaits must be shielded or
            # a disconnect can repeatedly cancel them and leak the media tasks.
            with CancelScope(shield=True):
                await asyncio.gather(*tasks, return_exceptions=True)
                with contextlib.suppress(OSError, RuntimeError, TimeoutError, WebSocketDisconnect):
                    await asyncio.wait_for(ws.close(), 2)
            # The manager owns cleanup; HTTP/WS cancellation must not cancel it.

    async def audio(self, reader, writer):
        task = asyncio.current_task()
        self.connections.add(task)
        call = None
        tasks = []

        async def read():
            async with asyncio.timeout(5):
                try:
                    header = await reader.readexactly(3)
                except asyncio.IncompleteReadError as error:
                    if error.partial:
                        raise
                    return 0, b""  # Asterisk may close without a termination packet.
                kind, length = struct.unpack("!BH", header)
                if length > 3200:
                    raise ValueError("Oversized AudioSocket frame")
                return kind, await reader.readexactly(length)

        try:
            kind, value = await read()
            active = self.active
            if (
                writer.get_extra_info("peername")[0] != "127.0.0.1"
                or kind != 1
                or len(value) != 16
                or not active
                or str(uuid.UUID(bytes=value)) != active.media_id
                or active.record["state"] not in {"dialing", "answering"}
                or active.stop.is_set()
                or active.writer
            ):
                return
            call = active
            call.writer = writer
            self.state(call, "connected")
            call.answered.set()

            async def receive():
                remainder = b""
                while True:
                    kind, value = await read()
                    if kind == 0:
                        return
                    if kind == 3 and len(value) == 1 and value in b"0123456789*#ABCD":
                        call.emit({"type": "dtmf", "digit": value.decode("ascii")})
                    elif kind == 0x10 and value and len(value) % 2 == 0:
                        remainder += value
                        while len(remainder) >= 320:
                            call.emit(remainder[:320])
                            remainder = remainder[320:]
                    else:
                        raise ValueError("Invalid AudioSocket frame")

            async def send():
                deadline = asyncio.get_running_loop().time()
                while True:
                    try:
                        value = call.to_phone.get_nowait()
                    except asyncio.QueueEmpty:
                        value = bytes(320)
                    writer.write(b"\x10\x01\x40" + value)
                    await asyncio.wait_for(writer.drain(), 2)
                    deadline = max(deadline + 0.02, asyncio.get_running_loop().time())
                    await asyncio.sleep(max(0, deadline - asyncio.get_running_loop().time()))

            tasks = [asyncio.create_task(receive()), asyncio.create_task(send()), asyncio.create_task(call.stop.wait())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if tasks[0] in done and tasks[0].exception() is None:
                call.end("phone_hangup")
            else:
                for item in done:
                    item.result()
        except ConnectionError:
            if call:
                call.end("phone_audio_disconnected")
        except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError):
            if call:
                call.end("phone_audio_error")
        finally:
            if call:
                call.end("phone_hangup")
            for item in tasks:
                item.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            writer.close()
            with contextlib.suppress(OSError, TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), 2)
            self.connections.discard(task)

    async def close(self):
        servers = [server for server in (self.incoming_server, self.server) if server]
        for server in servers:
            server.close()
        self.incoming_server = self.server = None
        if self.active:
            self.active.end("service_stopped")
            await asyncio.shield(self.active.task)
        for task in list(self.connections):
            task.cancel()
        await asyncio.gather(*self.connections, return_exceptions=True)
        # Newer asyncio waits for accepted connections here, not just listeners.
        # End calls and close their writers before awaiting server shutdown.
        for server in servers:
            await server.wait_closed()
        if self.lock_file:
            self.lock_file.close()
            self.lock_file = None

    def router(self):
        router = APIRouter(prefix=PREFIX, tags=["Calls"])

        @router.get("/openapi.json", include_in_schema=False)
        async def schema():
            document = get_openapi(title="Switchboard Calls", version="1", routes=router.routes)
            document["components"]["securitySchemes"] = {"CallToken": {"type": "http", "scheme": "bearer"}}
            document["security"] = [{"CallToken": []}]
            return document

        @router.get("/incoming", operation_id="incomingCalls")
        async def incoming():
            """Poll once per second for a stable inbound offer; reading never answers.

            Prepare audio before attaching the authenticated media WebSocket:
            attachment explicitly accepts. Offers expire after 30 seconds.
            """
            return self.inbox()

        @router.post("", status_code=201, operation_id="reserveCall")
        async def create(body: CallRequest, idempotency_key: Annotated[str, Header(alias="Idempotency-Key")]):
            """Reserve the configured handset; attach its media WebSocket within 15 seconds to ring.

            Persist a UUID Idempotency-Key before calling. Reuse that key and body
            after uncertainty; never retry with a new key. Audio is not a tool response.
            """
            return self.create(idempotency_key, body)

        @router.get("/{call_id}", operation_id="getCall")
        async def get(call_id: uuid.UUID):
            """Read one call's status without ringing or connecting audio."""
            return self.get(call_id)

        @router.delete("/{call_id}", operation_id="endCall")
        async def cancel(call_id: uuid.UUID):
            """Cancel or hang up this call. A late PBX channel can take up to 37 seconds to resolve."""
            return await self.cancel(call_id)

        @router.websocket("/{call_id}/media")
        async def media(ws: WebSocket, call_id: uuid.UUID):
            await self.websocket(ws, call_id)

        return router

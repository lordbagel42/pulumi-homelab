"""Adapters for the existing phone session owner and native Slack huddle bridge.

The platform owns configuration and orchestration; the bridges keep ownership of
their durable conversations and call media. Mutations are deliberately never
retried: a lost reply must not start another task or ring the handset twice.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
import uuid

import httpx


MAX_RESPONSE = 1024 * 1024


class ConnectorError(RuntimeError):
    """A safe public error from an integration, without credentials or payloads."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def _text(value: str, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must contain between 1 and {maximum} characters")
    return value.strip()


def _number(value: int | str) -> int:
    value = str(value).removeprefix("codex:").replace("-", "")
    if len(value) == 6 and value.startswith("611"):
        value = value[3:]
    if not value.isdecimal() or not 1 <= int(value) <= 999:
        raise ValueError("Use an existing session number from 001 to 999")
    return int(value)


class CodexConnector:
    """Talk directly to the bridge socket, never through the platform HTTP API."""

    def __init__(self, socket_path: str | Path, timeout: float = 135):
        self.socket_path = Path(socket_path).expanduser().resolve()
        self.timeout = timeout

    async def request(self, method: str, **params: Any) -> Any:
        writer = None
        try:
            wire = json.dumps({"method": method, "params": params}).encode() + b"\n"
            if len(wire) > MAX_RESPONSE:
                raise ValueError("Phone request is too large")
            async with asyncio.timeout(self.timeout):
                reader, writer = await asyncio.open_unix_connection(
                    str(self.socket_path), limit=MAX_RESPONSE)
                writer.write(wire)
                await writer.drain()
                line = await reader.readline()
                if not line.endswith(b"\n") or len(line) > MAX_RESPONSE:
                    raise ConnectorError("Phone bridge returned an incomplete or oversized reply")
                response = json.loads(line)
                if not isinstance(response, dict):
                    raise ConnectorError("Phone bridge returned an invalid reply")
                if "error" in response:
                    message = str(response["error"])[:1000]
                    status = 404 if "does not exist" in message or "Unknown phone question" in message else 409
                    raise ConnectorError(message, status)
                if "result" not in response:
                    raise ConnectorError("Phone bridge returned an invalid reply")
                return response["result"]
        except TimeoutError:
            raise ConnectorError("Phone bridge timed out; check session state before retrying", 504) from None
        except (OSError, ConnectionError):
            raise ConnectorError("Phone bridge is unavailable; check its service and control socket", 503) from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ConnectorError("Phone bridge returned an invalid reply") from None
        except ValueError as error:
            if "Separator is not found" in str(error) or "chunk is longer than limit" in str(error):
                raise ConnectorError("Phone bridge reply exceeded the size limit") from None
            raise
        finally:
            if writer:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()

    async def list_sessions(self) -> list[dict]:
        return await self.request("sessions")

    async def get_session(self, session_id: int | str) -> dict:
        number = _number(session_id)
        session = await self.request("session", session_id=number)
        try:
            questions = await self.request("questions", session_id=number)
            return {**session, "questions": questions, "questions_available": True}
        except ConnectorError as error:
            if str(error) != "Unknown control method":
                raise
            # An older deployed bridge is still usable until its next release.
            return {**session, "questions": [], "questions_available": False}

    async def create_operator(self, title: str, prompt: str, host: str = "proxmox",
                              cwd: str | None = None, engine: str | None = None) -> dict:
        title = _text(title, "Title", 200)
        prompt = _text(prompt, "Prompt", 20000)
        if host not in {"proxmox", "workstation"}:
            raise ValueError("Choose Proxmox or workstation")
        if engine is not None and engine not in {"amp", "codex"}:
            raise ValueError("Choose Amp or Codex")
        params = {"title": title, "host": host, "cwd": cwd}
        if engine is not None:
            params["engine"] = engine
        session = await self.request("create", **params)
        if engine == "amp" and session.get("engine") != "amp":
            raise ConnectorError(
                f"Session {session['id']} was allocated, but the bridge did not confirm Amp. "
                "The task was not submitted. Upgrade the phone bridge before starting Amp work.", 409)
        try:
            await self.send_input(session["id"], prompt)
        except ConnectorError as error:
            raise ConnectorError(
                f"Session {session['id']} was created, but its task submission could not be confirmed. "
                "Check that session before submitting again.", error.status_code) from error
        return await self.get_session(session["id"])

    async def register_session(self, session_key: str, title: str, cwd: str = "") -> dict:
        return await self.request("register", session_key=_text(session_key, "Session key", 300),
                                  title=_text(title, "Title", 200), cwd=cwd)

    async def update_session(self, session_id: int | str, summary: str, state: str = "running") -> dict:
        if state not in {"idle", "running", "waiting", "done", "error", "interrupted"}:
            raise ValueError("Invalid session state")
        return await self.request("update", session_id=_number(session_id),
                                  summary=_text(summary, "Summary", 5000), state=state)

    async def send_input(self, session_id: int | str, text: str) -> dict:
        return await self.request("submit", session_id=_number(session_id), text=_text(text, "Input", 20000))

    async def cancel(self, session_id: int | str) -> dict:
        return await self.request("interrupt", session_id=_number(session_id))

    async def request_callback(self, session_id: int | str, message: str | None = None,
                               request_key: str | None = None) -> dict:
        if message is not None:
            return await self.ask_question(session_id, [{"id": "answer", "header": "Callback",
                "question": _text(message, "Callback message", 3000), "options": []}], request_key)
        return await self.request("ring", session_id=_number(session_id))

    async def ask_question(self, session_id: int | str, questions: list[dict],
                           request_key: str | None = None) -> dict:
        if request_key is not None:
            request_key = _text(request_key, "Request key", 300)
        return await self.request("ask", session_id=_number(session_id),
                                  questions=questions, request_key=request_key)

    async def answer_question(self, session_id: int | str, question_id: str,
                              answer: str, item_id: str | None = None) -> dict:
        question = await self.request("question", question_id=question_id, wait_seconds=0)
        if question["session_id"] != _number(session_id):
            raise ValueError("That question belongs to another session")
        if item_id is None:
            remaining = [item["id"] for item in question["questions"]
                         if item["id"] not in question["answers"]]
            if len(remaining) != 1:
                raise ValueError("Specify item_id when a question has multiple unanswered items")
            item_id = remaining[0]
        return await self.request("answer", question_id=question_id, item_id=item_id,
                                  text=_text(answer, "Answer", 20000))

    async def inbox(self, session_id: int | str, acknowledge: bool = False) -> list[dict]:
        return await self.request("inbox", session_id=_number(session_id), acknowledge=acknowledge)

    async def health(self) -> dict:
        try:
            async with asyncio.timeout(min(3, self.timeout)):
                sessions = await self.list_sessions()
                try:
                    capabilities = await self.request("capabilities")
                except ConnectorError as error:
                    if str(error) != "Unknown control method":
                        raise
                    capabilities = {}
            return {"healthy": True, "status": "connected", "detail": "Phone session bridge connected",
                    "session_count": len(sessions), "capabilities": capabilities}
        except (ConnectorError, TimeoutError):
            return {"healthy": False, "status": "offline", "detail": "Phone session bridge unavailable"}


class HuddleConnector:
    """Authenticated control plane; media stays in the existing huddle service."""

    def __init__(self, base_url: str, token: str, timeout: float = 20,
                 transport: httpx.AsyncBaseTransport | None = None):
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Huddle URL must be an HTTP(S) base URL without credentials or a query")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout = timeout
        self._transport = transport

    async def _request(self, path: str, body: dict | None = None) -> dict:
        headers = {"Authorization": "Bearer " + self._token} if self._token else {}
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False,
                                         trust_env=False, transport=self._transport) as client:
                async with client.stream("POST" if body is not None else "GET",
                                         self.base_url + path, json=body, headers=headers) as response:
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_RESPONSE:
                            raise ConnectorError("Huddle bridge reply exceeded the size limit")
                    if response.status_code in {401, 403}:
                        raise ConnectorError("Huddle bridge credentials were rejected", 503)
                    if response.status_code == 404:
                        raise ConnectorError("Huddle request was not found", 404)
                    if response.status_code == 409:
                        raise ConnectorError("Huddle bridge is busy or this request conflicts with an existing call", 409)
                    if response.status_code >= 300 and not (path == "/healthz" and response.status_code == 503):
                        raise ConnectorError(f"Huddle bridge returned HTTP {response.status_code}",
                                             400 if response.status_code == 400 else 502)
                    result = json.loads(data)
                    if not isinstance(result, dict):
                        raise ConnectorError("Huddle bridge returned an invalid reply")
                    return result
        except httpx.TimeoutException:
            raise ConnectorError("Huddle bridge timed out; check call status before retrying", 504) from None
        except httpx.HTTPError:
            raise ConnectorError("Huddle bridge is unavailable", 503) from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ConnectorError("Huddle bridge returned an invalid reply") from None

    async def command(self, command: str, **params: Any) -> dict:
        if command not in {"lookup", "dial", "status", "cancel", "recent"}:
            raise ValueError("Unknown huddle command")
        if not self._token:
            raise ConnectorError("Configure the huddle bridge token first", 503)
        result = await self._request("/control", {**params, "command": command})
        # Native status omits the caller's durable key; return it so generic
        # clients can safely inspect an ambiguous dial response without redial.
        if command in {"dial", "status", "cancel"} and params.get("request_id"):
            result = {**result, "request_id": params["request_id"]}
        return result

    async def lookup(self, query: str) -> dict:
        return await self.command("lookup", query=_text(query, "Search", 200))

    async def recent(self) -> dict:
        return await self.command("recent")

    async def dial(self, user_id: str, request_id: str | None = None, mode: str = "ring") -> dict:
        if mode not in {"ring", "operator"}:
            raise ValueError("Choose ring or operator call mode")
        return await self.command("dial", user_id=_text(user_id, "Slack member ID", 100),
                                  request_id=str(uuid.UUID(request_id)) if request_id else str(uuid.uuid4()), mode=mode)

    async def status(self, request_id: str) -> dict:
        return await self.command("status", request_id=str(uuid.UUID(request_id)))

    async def cancel(self, request_id: str) -> dict:
        return await self.command("cancel", request_id=str(uuid.UUID(request_id)))

    async def health(self) -> dict:
        try:
            async with asyncio.timeout(min(3, self.timeout)):
                result = await self._request("/healthz")
            healthy = result.get("healthy") is True
            return {**result, "healthy": healthy,
                    "status": "connected" if healthy else "degraded",
                    "detail": "Native Slack huddle bridge connected" if healthy else "Huddle bridge is not ready",
                    "control_configured": bool(self._token)}
        except (ConnectorError, TimeoutError):
            return {"healthy": False, "status": "offline", "detail": "Huddle bridge unavailable",
                    "control_configured": bool(self._token)}

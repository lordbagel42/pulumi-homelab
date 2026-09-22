"""Private, owner-only Unix socket API shared by the bridge, CLI, and MCP."""

import asyncio
import contextlib
import json
import os
import socket
import struct
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

from session_store import session_number
from callback_policy import callback_permitted

# Deployment releases can exceed Unix socket path limits; resolve their shared
# state symlink to the short persistent directory before opening the socket.
SOCKET = Path(os.environ.get("CODEX_PHONE_SOCKET", Path(__file__).resolve().parent / "state/control.sock")).resolve()


def platform_settings():
    """Optional private client registration, read afresh for each tool request."""
    path = Path(os.environ.get("PHONE_PLATFORM_CLIENT_CONFIG", Path(__file__).resolve().parent / "state/platform.json"))
    data = json.loads(path.read_text()) if path.exists() else {}
    return {
        "url":os.environ.get("PHONE_PLATFORM_URL",data.get("url", "")),
        "token_file":os.environ.get("PHONE_PLATFORM_TOKEN_FILE",data.get("token_file", "")),
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


async def platform_request(path, payload=None, *, method=None, idempotency_key=None):
    """Use the configured central API without ever forwarding its token elsewhere."""
    settings = platform_settings()
    base = settings["url"].rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Set PHONE_PLATFORM_URL to the central HTTP(S) base URL")
    token_path = settings["token_file"]
    if not token_path:
        raise ValueError("Set PHONE_PLATFORM_TOKEN_FILE for the central phone API")
    token = Path(token_path).read_text().strip()
    if not token or "\n" in token or "\r" in token:
        raise ValueError("The phone platform token file must contain one token")

    def send():
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        request = urllib.request.Request(base + path, data=json.dumps(payload).encode() if payload is not None else None,
            headers=headers, method=method)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(request, timeout=135) as response:
                data = response.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise RuntimeError("Phone platform reply was too large")
            return json.loads(data)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"Phone platform returned HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError):
            raise RuntimeError("Phone platform is unavailable; check session state before retrying") from None
    return await asyncio.to_thread(send)


async def request(method, **params):
    if platform_settings()["url"]:
        response = await platform_request("/api/v1/phone/rpc", {"method": method, "params": params})
        if "error" in response:
            raise RuntimeError(response["error"])
        return response["result"]
    reader, writer = await asyncio.open_unix_connection(str(SOCKET), limit=1024 * 1024)
    try:
        writer.write((json.dumps({"method": method, "params": params}) + "\n").encode())
        await writer.drain()
        response = json.loads(await asyncio.wait_for(reader.readline(), 135))
        if "error" in response:
            raise RuntimeError(response["error"])
        return response["result"]
    finally:
        writer.close()
        await writer.wait_closed()


class Control:
    def __init__(self, sessions, amp_controls=None):
        self.sessions = sessions
        self.amp_controls = amp_controls

    async def dispatch(self, method, args):
        sessions, store = self.sessions, self.sessions.store
        if method == "sessions":
            return store.list_sessions()
        if method == "capabilities":
            return sessions.capabilities()
        if method in {"amp_poll", "amp_ack"}:
            if self.amp_controls is None:
                raise ValueError("Amp runner controls are not configured")
            if method == "amp_poll":
                return await self.amp_controls.poll(**args)
            return self.amp_controls.ack(**args)
        if method == "session":
            return store.get_session(args["session_id"])
        if method == "questions":
            number = session_number(args["session_id"]) if args.get("session_id") is not None else None
            if number is not None:
                store.get_session(number)
            return store.pending_questions(number)
        if method == "create":
            return sessions.create(args.get("title", "Phone task"), args.get("cwd"), args.get("host", "proxmox"), args.get("engine"))
        if method == "select_host":
            return sessions.select_host(args["session_id"], args["host"])
        if method == "submit":
            return {"message": await sessions.submit(args["session_id"], args["text"])}
        if method == "interrupt":
            return {"message": await sessions.interrupt(args["session_id"])}
        if method == "register":
            key = args["session_key"]
            if not isinstance(key, str) or not 1 <= len(key) <= 300:
                raise ValueError("Provide a stable session key")
            return store.create_session(args.get("title", "Desktop Codex task"),
                                        args.get("cwd", ""), kind="external", external_key=key)
        if method == "update":
            session = store.get_session(args["session_id"])
            if session["kind"] != "external":
                raise ValueError("Only externally managed session status can be published")
            state = args.get("state", "running")
            if state not in ("idle", "running", "waiting", "done", "error", "interrupted"):
                raise ValueError("Invalid session state")
            store.update_session(session["id"], state=state, progress=args["summary"][:5000],
                                 last_reply=args["summary"][:5000])
            sessions.changed(session["id"])
            return store.get_session(session["id"])
        if method == "ask":
            return sessions.ask_question(args["session_id"], args["questions"], "mcp", args.get("request_key"))
        if method == "answer":
            return await sessions.answer(args["question_id"], args["item_id"], args["text"])
        if method == "question":
            return await sessions.wait_answer(args["question_id"], args.get("wait_seconds", 0))
        if method == "inbox":
            number = session_number(args["session_id"])
            store.get_session(number)
            return store.inbox(number, bool(args.get("acknowledge", True)))
        if method == "ring":
            number = session_number(args["session_id"])
            store.get_session(number)
            if any(sessions.attached.values()) or await sessions.pbx.phone_busy():
                raise ValueError("The handset is busy")
            if not await callback_permitted():
                raise ValueError("Quiet hours are active or callback policy is unavailable")
            return {"call_file": await sessions.pbx.ring(number), "extension": f"611{number:03d}"}
        raise ValueError("Unknown control method")

    async def accept(self, reader, writer):
        try:
            peer = writer.get_extra_info("socket")
            _, uid, _ = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.getuid():
                raise PermissionError("Only the bridge owner may use the phone control socket")
            line = await asyncio.wait_for(reader.readline(), 5)
            message = json.loads(line)
            result = await self.dispatch(message["method"], message.get("params", {}))
            response = {"result": result}
        except asyncio.CancelledError:
            raise
        except Exception as error:
            response = {"error": str(error)}
        try:
            writer.write((json.dumps(response) + "\n").encode())
            await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def start(self):
        SOCKET.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        SOCKET.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(self.accept, str(SOCKET), limit=1024 * 1024)
        SOCKET.chmod(0o600)
        return server

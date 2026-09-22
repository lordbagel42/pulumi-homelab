"""Small authenticated API client for operators and new phone integrations.

Credentials come from a private file, never from a task prompt or command-line
argument. Run ``python -m phone_platform.client --help`` for JSON-producing tools.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import urllib.error
from urllib.parse import quote, urlsplit
import urllib.request
import uuid


class ClientError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, base_url: str | None = None, token_file: str | Path | None = None):
        registration = Path(os.environ.get("PHONE_PLATFORM_CLIENT_CONFIG", Path(__file__).resolve().parents[2] / "voice/state/platform.json"))
        saved = json.loads(registration.read_text()) if registration.exists() else {}
        self.base_url = (base_url or os.environ.get("PHONE_PLATFORM_URL") or saved.get("url") or "http://127.0.0.1:8088").rstrip("/")
        parsed = urlsplit(self.base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ClientError("Use a phone platform HTTP(S) URL without embedded credentials")
        self.token_file = Path(token_file or os.environ.get("PHONE_PLATFORM_TOKEN_FILE") or
            saved.get("token_file") or "/var/lib/codex-phone/switchboard/admin-token")

    def request(self, method: str, path: str, data: dict | None = None):
        if not path.startswith("/api/v1/") or ".." in path:
            raise ClientError("Use a phone platform API path")
        try:
            token = self.token_file.read_text().strip()
        except OSError:
            raise ClientError("The phone platform token file is unavailable") from None
        if not token or "\n" in token or "\r" in token:
            raise ClientError("The phone platform token file must contain one token")
        request = urllib.request.Request(self.base_url + path, method=method,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(request, timeout=135) as response:
                body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise ClientError("Phone platform reply exceeded the size limit")
            return json.loads(body)
        except urllib.error.HTTPError as error:
            # Provider error bodies can contain credentials or internal URLs.
            raise ClientError(f"Phone platform returned HTTP {error.code}; inspect status before retrying a call") from None
        except (urllib.error.URLError, TimeoutError):
            raise ClientError("Phone platform is unavailable; inspect state before retrying a mutation") from None
        except (ValueError, UnicodeDecodeError):
            raise ClientError("Phone platform returned invalid JSON") from None

    def huddle(self, command: str, **params):
        return self.request("POST", "/api/v1/integrations/slack-huddles/control", {**params, "command": command})

    def operator(self, title: str, prompt: str, *, host: str = "proxmox", cwd: str | None = None,
                 instructions: str = "", integration: str | None = None, engine: str | None = None):
        return self.request("POST", "/api/v1/operators", {"title": title, "prompt": prompt,
            "host": host, "cwd": cwd, "instructions": instructions, "integration": integration or "amp",
            **({"engine":engine} if engine is not None else {})})

    def session(self, session_id: str):
        return self.request("GET", "/api/v1/sessions/" + quote(str(session_id), safe=""))

    def input(self, session_id: str, text: str):
        return self.request("POST", "/api/v1/sessions/" + quote(str(session_id), safe="") + "/input", {"text": text})

    def cancel(self, session_id: str):
        return self.request("POST", "/api/v1/sessions/" + quote(str(session_id), safe="") + "/cancel", {})


def parser():
    root = argparse.ArgumentParser(description="Phone platform tools. Output is JSON; credentials stay in a private file.")
    groups = root.add_subparsers(dest="group", required=True)
    huddle = groups.add_parser("huddle", help="Look up Slack members and control a native huddle").add_subparsers(dest="action", required=True)
    lookup = huddle.add_parser("lookup")
    lookup.add_argument("query")
    dial = huddle.add_parser("dial")
    dial.add_argument("user_id")
    dial.add_argument("--request-id", required=True, help="A stable UUID for this one call attempt; never replace it to retry")
    dial.add_argument("--mode", choices=["ring", "operator"], default="ring")
    dial.add_argument("--operator-call-id", help="Live phone call UUID supplied by its operator")
    for action in ("status", "cancel"):
        huddle.add_parser(action).add_argument("request_id")
    operator = groups.add_parser("operator", help="Create a persistent Amp operator")
    operator.add_argument("title")
    operator.add_argument("prompt")
    operator.add_argument("--host", choices=["proxmox", "workstation"], default="proxmox")
    operator.add_argument("--cwd")
    operator.add_argument("--instructions", default="")
    operator.add_argument("--integration")
    operator.add_argument("--engine", choices=["amp", "codex"], help="Use Amp or explicitly select legacy Codex")
    sessions = groups.add_parser("session", help="Inspect, steer or stop persistent sessions").add_subparsers(dest="action", required=True)
    sessions.add_parser("list")
    for action in ("get", "cancel", "input", "callback"):
        command = sessions.add_parser(action)
        command.add_argument("session_id")
        if action in {"input", "callback"}:
            command.add_argument("text")
    answer = groups.add_parser("answer", help="Answer a pending clarification question")
    answer.add_argument("question_id")
    answer.add_argument("text")
    answer.add_argument("--item-id", default="answer")
    groups.add_parser("uuid", help="Generate one durable request ID before a huddle call")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.group == "uuid":
            result = {"request_id": str(uuid.uuid4())}
        else:
            client = Client()
            if args.group == "huddle":
                if args.action == "lookup":
                    result = client.huddle("lookup", query=args.query)
                elif args.action == "dial":
                    request_id = str(uuid.UUID(args.request_id))
                    params = {"user_id":args.user_id,"request_id":request_id,"mode":args.mode}
                    if args.operator_call_id:
                        params["operator_call_id"] = str(uuid.UUID(args.operator_call_id))
                    result = client.huddle("dial", **params)
                else:
                    result = client.huddle(args.action, request_id=str(uuid.UUID(args.request_id)))
            elif args.group == "operator":
                result = client.operator(args.title, args.prompt, host=args.host, cwd=args.cwd,
                    instructions=args.instructions, integration=args.integration, engine=args.engine)
            elif args.group == "answer":
                result = client.request("POST", "/api/v1/questions/" + quote(args.question_id, safe="") + "/answer",
                                        {"item_id": args.item_id, "text": args.text})
            elif args.action == "list":
                result = client.request("GET", "/api/v1/sessions")
            elif args.action == "get":
                result = client.session(args.session_id)
            elif args.action == "cancel":
                result = client.cancel(args.session_id)
            elif args.action == "input":
                result = client.input(args.session_id, args.text)
            else:
                result = client.request("POST", "/api/v1/sessions/" + quote(args.session_id, safe="") + "/callback",
                                        {"question": args.text})
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ClientError, ValueError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

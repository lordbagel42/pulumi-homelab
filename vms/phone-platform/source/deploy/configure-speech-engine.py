#!/usr/bin/env python3
"""Configure separate operator engines via the authenticated Switchboard API.

Run after deployment. Reuses saved ElevenLabs credentials without printing them.
Resource provisioning is billable; activation changes the next call's provider.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import urllib.error
import urllib.request
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream-url", required=True)
    parser.add_argument("--activate", action="store_true")
    args = parser.parse_args()
    parsed = urlsplit(args.upstream_url)
    if (parsed.scheme != "wss" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path != "/speech-engine/upstream"):
        parser.error("Use the public wss URL ending in /speech-engine/upstream")
    state = Path("/var/lib/codex-phone/switchboard")
    token = (state / "admin-token").read_text().strip()

    def api(path, payload=None, method=None):
        request = urllib.request.Request("http://127.0.0.1:8088/api/v1" + path,
            data=None if payload is None else json.dumps(payload).encode(),
            method=method or ("POST" if payload is not None else "GET"),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            # Provider errors or credentials must never appear in deployment logs.
            raise SystemExit(f"Switchboard configuration failed (HTTP {error.code}); inspect the dashboard before retrying") from None

    with sqlite3.connect("file:" + str(state / "switchboard.sqlite3") + "?mode=ro", uri=True) as db:
        key = ""
        for provider in ("elevenlabs-speech-engine", "elevenlabs-tts", "elevenlabs-stt"):
            row = db.execute("SELECT value FROM credentials WHERE id=?", (provider,)).fetchone()
            if row and row[0]:
                key = row[0]
                break
    if not key:
        raise SystemExit("Save an ElevenLabs API key in Switchboard first")
    integrations = {i["id"]: i for i in api("/integrations")["integrations"]}
    config = integrations["elevenlabs-speech-engine"]["config"]
    previous = integrations["elevenlabs-tts"]["config"]
    config.update(upstream_url=args.upstream_url,
                  voice_id=config.get("voice_id") or previous.get("voice_id", ""))
    if not config["voice_id"]:
        raise SystemExit("Choose an ElevenLabs voice in Switchboard first")
    api("/integrations/elevenlabs-speech-engine", {"enabled": True, "api_key": key, "config": config}, "PUT")
    routes = {r["id"]: r for r in api("/routes")["routes"]}
    for route_id in ("codex", "slack-operator"):
        route = routes[route_id]
        route.setdefault("speech_mode", "legacy")
        route.setdefault("speech_engine", {})
        api("/routes/" + route_id, route, "PUT")
        api("/speech/engine/provision/" + route_id, {})
        print(route_id + ": Speech Engine configured")
    if args.activate:
        # Re-read IDs written by provisioning; never overwrite them with stale data.
        for route in api("/routes")["routes"]:
            if route["id"] in ("codex", "slack-operator"):
                route["speech_mode"] = "speech-engine"
                api("/routes/" + route["id"], route, "PUT")
        print("Both operator routes now select Speech Engine for their next call")


if __name__ == "__main__":
    main()

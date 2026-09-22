#!/usr/bin/env python3
"""Refuse disruptive phone service changes while a call or agent task is active."""
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import urllib.error
import urllib.request


def running(container):
    result = subprocess.run(["docker", "inspect", "--format", "{{.State.Running}}", container],
                            capture_output=True, text=True)
    return result.returncode == 0 and result.stdout.strip() == "true"


def main():
    if running("sccp-pbx"):
        channels = subprocess.check_output(["docker", "exec", "sccp-pbx", "asterisk", "-rx",
                                             "core show channels count"], text=True)
        counts = re.findall(r"(\d+) active (?:channels?|calls?)", channels)
        if not counts or any(int(count) for count in counts):
            raise SystemExit("Phone is in use; deployment stopped before service restart.")
    database = Path("/var/lib/codex-phone/state/sessions.sqlite3")
    if database.is_file():
        with sqlite3.connect("file:" + str(database) + "?mode=ro", uri=True, timeout=5) as db:
            # A 'choose' session deliberately waits for the user to select a
            # host. Its queued work persists across restarts and cannot run yet.
            if db.execute("""SELECT 1 FROM jobs j JOIN sessions s ON s.id=j.session_id
                WHERE j.state='running' OR (j.state='queued' AND s.host!='choose') LIMIT 1""").fetchone():
                raise SystemExit("A Codex phone task is active; deployment stopped before service restart.")
    try:
        with urllib.request.urlopen("http://127.0.0.1:8099/healthz", timeout=5) as response:
            huddle = json.load(response)
        if huddle.get("call_phase") != "idle":
            raise SystemExit("A Huddle call is active; deployment stopped before service restart.")
    except (urllib.error.URLError, TimeoutError, OSError):
        if running("huddle-phone-huddle-phone-1"):
            raise SystemExit("Cannot verify Huddle idle state; deployment stopped before service restart.")
    print("Phone, Huddles and Codex tasks are idle.")


if __name__ == "__main__":
    main()

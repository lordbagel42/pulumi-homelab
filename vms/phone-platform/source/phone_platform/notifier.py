"""Optional Linux desktop companion. No browser permission or open tab required."""
import argparse
import json
import logging
import os
from pathlib import Path
import subprocess
import time
import threading
from urllib.parse import quote
from .client import Client, ClientError

LOG = logging.getLogger("switchboard.notifier")


def attention_key(item):
    questions = sorted(str(q["id"]) for q in item.get("questions", []))
    return item["id"] + ":" + (",".join(questions) if questions else str(item["state"])+":"+str(item.get("updated", "")))


def poll(client, state_path, seen):
    data = client.request("GET", "/api/v1/attention")
    for item in data["items"]:
        key = attention_key(item)
        if key in seen:
            continue
        # Do not include task contents on the lock screen.
        target = client.base_url+"/#attention/"+quote(item["id"], safe="")
        process = subprocess.Popen(["notify-send", "--app-name=Switchboard", "--action=default=Open question", "--expire-time=15000",
            "Switchboard needs your attention", "Open the attention inbox to review a waiting task."],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        def opened(process=process, target=target):
            try:
                output, _ = process.communicate(timeout=60)
                if process.returncode == 0 and output.strip() == "default":
                    subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
        threading.Thread(target=opened, daemon=True).start()
        seen.append(key)
        seen[:] = seen[-500:]
        state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(seen))
        temporary.chmod(0o600)
        temporary.replace(state_path)
        LOG.info("Desktop notification submitted")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--open", action="store_true", help="Open the attention inbox")
    args = parser.parse_args()
    client = Client()
    if args.open:
        subprocess.run(["xdg-open", client.base_url+"/#attention"], check=True)
        return
    path = Path(os.environ.get("XDG_STATE_HOME", Path.home()/".local/state"))/"switchboard/notifications.json"
    try:
        seen = json.loads(path.read_text())
        if not isinstance(seen,list):
            seen = []
    except (OSError, ValueError):
        seen = []
    logging.basicConfig(level=logging.INFO)
    while True:
        try:
            poll(client, path, seen)
        except (ClientError, OSError, ValueError, subprocess.SubprocessError):
            LOG.warning("Notification check failed; will reconnect without replaying delivered notices")
            if args.once:
                raise
        if args.once:
            return
        time.sleep(5)


if __name__ == "__main__":
    main()

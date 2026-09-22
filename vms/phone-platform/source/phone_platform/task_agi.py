"""Asterisk AGI adapter for 89xxxx saved tasks; install only during PBX deployment.

Run with the Switchboard package and a private client registration available to
its service user. Asterisk passes the requested number as the first argument.
The channel unique ID is an idempotency key; an ambiguous launch is never retried.
"""
import json
import re
import sys
import urllib.request
from .client import Client, _NoRedirect


def main():
    env = {}
    for line in sys.stdin:
        if not line.strip():
            break
        key, _, value = line.partition(":")
        env[key.strip()] = value.strip()
    number = sys.argv[1] if len(sys.argv)>1 else ""
    if not re.fullmatch(r"89\d{4}", number) or not env.get("agi_uniqueid"):
        raise ValueError("A saved-task extension and channel ID are required")
    client = Client()
    request = urllib.request.Request(client.base_url+"/api/v1/task-templates/"+number+"/run",
        data=b"{}", headers={"Authorization":"Bearer "+client.token_file.read_text().strip(),
        "Content-Type":"application/json", "Idempotency-Key":"agi:"+env["agi_uniqueid"]})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(request, timeout=30) as response:
        session = json.loads(response.read(1024*1024))
    extension = session["extension"]
    if not re.fullmatch(r"611\d{3}", extension):
        raise ValueError("Unexpected callback extension")
    print('SET VARIABLE SWITCHBOARD_TASK_EXTENSION "'+extension+'"', flush=True)
    sys.stdin.readline()


if __name__ == "__main__":
    main()

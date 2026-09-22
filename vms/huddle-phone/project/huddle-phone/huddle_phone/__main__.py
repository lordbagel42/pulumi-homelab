import argparse
import asyncio
import fcntl
import logging
import os
import uuid
from pathlib import Path

from .config import Config
from .slack import SlackError


async def control(config, command, value):
    import aiohttp
    if not config.operator_secret:
        raise ValueError("Set OPERATOR_SECRET to enable outgoing calls")
    if not value:
        raise ValueError("Supply a name for lookup or a Slack member ID for dial")
    body = ({"command": "lookup", "query": value} if command == "lookup" else
            {"command": "dial", "user_id": value, "request_id": str(uuid.uuid4()), "mode": "ring"})
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as http:
        async with http.post(f"http://127.0.0.1:{config.http_port}/control", json=body,
                             headers={"Authorization": "Bearer " + config.operator_secret}) as response:
            if response.status != 200:
                raise ValueError(f"Operator request failed (HTTP {response.status}); check the member ID and bridge availability")
            result = await response.json()
    if command == "lookup":
        for user in result["users"]:
            print(f"{user['id']}  {user['name']}  @{user['username']}")
        if result["more"]:
            print("More matches exist; use a more specific name or username.")
    else:
        print("Calling your handset. Answer to start the Slack huddle and invite that member.")
        print("Request ID: " + body["request_id"])


async def doctor(config):
    import aiohttp
    from .slack import Slack
    from .ami import AMI
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
        await Slack(config, http).authenticate()
        print("Slack user session, workspace and channel membership: OK")
    if not config.dry_run:
        ami = AMI(config)
        try:
            await ami.connect()
            await ami.action([("Action", "Ping")])
            print("Asterisk AMI login: OK")
        finally:
            await ami.close()
    print("No huddle joined and no phone call placed. Run the live acceptance test in README.md next.")


def main():
    parser = argparse.ArgumentParser(description="Forward Slack channel huddles and direct invitations to your phone")
    parser.add_argument("command", choices=("run", "doctor", "lookup", "dial"), default="run", nargs="?")
    parser.add_argument("value", nargs="?")
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        config = Config.from_env()
        if args.command in {"lookup", "dial"}:
            asyncio.run(control(config, args.command, args.value))
            return
        if args.command == "doctor":
            asyncio.run(doctor(config))
            return
        Path(config.state_path).parent.mkdir(parents=True, exist_ok=True)
        with open(str(Path(config.state_path).parent / "process.lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            from .app import Service
            asyncio.run(Service(config).run())
    except (ValueError, SlackError) as error:
        logging.error("Setup failed: %s", error)
        raise SystemExit(2)
    except BlockingIOError:
        logging.error("Another bot process holds this state volume; run only one replica")
        raise SystemExit(2)
    except Exception as error:
        logging.error("Service failed error_type=%s; check the deployment guide", type(error).__name__)
        raise SystemExit(1)


if __name__ == "__main__":
    main()

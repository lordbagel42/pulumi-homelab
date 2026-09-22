#!/usr/bin/env python3
"""Save private Home Assistant phone settings, or check the connection read-only."""

import argparse
import asyncio
import getpass
import json
import os
import tempfile
from pathlib import Path

from home_assistant import CONFIG_PATH, Config, HomeAssistant, HomeAssistantError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path,
                        default=Path(os.environ.get("HOME_ASSISTANT_CONFIG", CONFIG_PATH)))
    parser.add_argument("--check", action="store_true", help="Check the saved API connection without controlling devices")
    args = parser.parse_args()
    path = args.config.expanduser()
    try:
        if args.check:
            config = Config.load(path)
        else:
            url = input("Home Assistant URL (for example http://homeassistant.local:8123): ").strip()
            print("Create a Long-Lived Access Token in your Home Assistant profile's Security tab.")
            token = getpass.getpass("Access token (hidden): ").strip()
            config = Config(url, token)
        asyncio.run(HomeAssistant(config).check())
        if not args.check:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".home-assistant-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as output:
                    json.dump({"url": config.url, "token": config.token,
                               "agent_id": config.agent_id}, output, indent=2)
                    output.write("\n")
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)
            print(f"Saved private configuration to {path} (mode 600).")
        print("Home Assistant API connected. Dial 555 and speak after the greeting.")
        print("Devices must be exposed to Assist in Settings > Voice assistants > Expose.")
    except (HomeAssistantError, OSError) as error:
        parser.exit(1, f"Setup failed: {error}\n")
    except (KeyboardInterrupt, EOFError):
        parser.exit(1, "Setup cancelled.\n")


if __name__ == "__main__":
    main()

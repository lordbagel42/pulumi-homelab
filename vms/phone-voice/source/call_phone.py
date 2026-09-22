#!/usr/bin/env python3
"""Ring this owner's Cisco phone once, connecting an answer to Codex."""

import argparse
import asyncio

from control import request


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", nargs="?", help="Existing callback number, e.g. 611001; omit for a new session")
    parser.add_argument("--host", choices=["choose", "proxmox", "workstation"], default="choose")
    args = parser.parse_args()
    session = (await request("session", session_id=args.session)) if args.session else (await request("create", host=args.host))
    print("Persistent callback number:", session["display_number"], flush=True)
    result = await request("ring", session_id=session["id"])
    print("Calling the owner's Cisco phone once, for up to 45 seconds.")
    print("Call record:", result["call_file"])


if __name__ == "__main__":
    asyncio.run(main())

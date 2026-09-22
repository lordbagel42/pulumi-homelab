#!/usr/bin/env python3
"""Small local control client; never prints credentials."""

import argparse
import asyncio
import json

from control import request


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("method", choices=["sessions", "session", "create", "select_host", "submit", "interrupt", "ring", "question"])
    parser.add_argument("--host", choices=["proxmox", "workstation", "choose"])
    parser.add_argument("--session-id")
    parser.add_argument("--text")
    parser.add_argument("--title")
    parser.add_argument("--cwd")
    parser.add_argument("--question-id")
    args = vars(parser.parse_args())
    method = args.pop("method")
    print(json.dumps(await request(method, **{k: v for k, v in args.items() if v is not None}), indent=2))


if __name__ == "__main__":
    asyncio.run(main())

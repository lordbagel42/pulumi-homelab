#!/usr/bin/env python3
"""Compatibility entry point for the persistent phone bridge."""
import asyncio
import logging
from audio import ROOT, Speech, packet, read_packet
from phone_bridge import main

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(main())

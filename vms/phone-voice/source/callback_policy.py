"""Optional Switchboard quiet-hours gate for every native callback.

Enable PHONE_PLATFORM_CALLBACK_POLICY=1 when deploying this bridge version with
Switchboard's /quiet-hours API. A failed policy read defers callbacks rather than
ringing during an unknown schedule. Questions stay answerable in the dashboard.
"""
import asyncio
import logging
import os

LOG = logging.getLogger("codex-phone.callback-policy")


async def callback_permitted():
    if os.environ.get("PHONE_PLATFORM_CALLBACK_POLICY") != "1":
        return True
    from control import platform_request
    try:
        settings = await asyncio.wait_for(platform_request("/api/v1/quiet-hours", method="GET"), 5)
        if not isinstance(settings.get("active"), bool):
            raise ValueError("Invalid callback policy")
        return not settings["active"]
    except (OSError, ValueError, RuntimeError, TimeoutError):
        LOG.warning("Callback deferred: Switchboard quiet-hours policy is unavailable")
        return False

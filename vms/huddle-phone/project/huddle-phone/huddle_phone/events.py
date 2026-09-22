"""Slack's authenticated user event feed, including direct huddle invitations.

The Enterprise client uses client.getWebSocketURL, not legacy rtm.connect.
Socket URLs contain credentials: never log URLs or transport exception strings.
"""

import asyncio
import json
import logging
import random
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp

from .slack import SlackError

LOG = logging.getLogger("huddle-phone")
FATAL_AUTH = {"invalid_auth", "token_revoked", "account_inactive", "not_authed", "token_expired"}


def socket_url(base, token):
    if not isinstance(base, str):
        raise SlackError("client.getWebSocketURL", "invalid_socket_url")
    try:
        parts = urlsplit(base)
        valid = (parts.scheme == "wss" and parts.hostname in {"wss-primary.slack.com", "wss-backup.slack.com"}
                 and parts.port in (None, 443) and not parts.username and not parts.password and not parts.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise SlackError("client.getWebSocketURL", "invalid_socket_url")
    query = [(key, value) for key, value in parse_qsl(parts.query) if key != "token"]
    return urlunsplit(parts._replace(query=urlencode([*query, ("token", token)])))


class Invitations:
    def __init__(self, slack, handle, stopped):
        self.slack, self.handle, self.stopped = slack, handle, stopped
        self.connected = False

    async def connection(self):
        response = await self.slack.api("client.getWebSocketURL")
        url = socket_url(response.get("primary_websocket_url"), self.slack.config.client_token)
        async with self.slack.http.ws_connect(
            url, headers={"Cookie": self.slack.config.cookie, "Origin": "https://app.slack.com"},
            heartbeat=20, max_msg_size=4 * 1024 * 1024,
        ) as socket:
            hello = await socket.receive_json(timeout=15)
            if not isinstance(hello, dict) or hello.get("type") != "hello":
                raise SlackError("events", "missing_hello")
            self.connected = True
            LOG.info("Listening for direct Slack huddle invitations")

            async def ping():
                number = 0
                while True:
                    await asyncio.sleep(20)
                    number += 1
                    await socket.send_json({"id": number, "type": "ping"})

            pings = asyncio.create_task(ping())
            try:
                while not self.stopped.is_set():
                    message = await socket.receive(timeout=45)
                    if message.type != aiohttp.WSMsgType.TEXT:
                        break
                    event = json.loads(message.data)
                    if not isinstance(event, dict):
                        continue
                    kind = event.get("type")
                    if kind in ("goodbye", "reconnect_url"):
                        if kind == "goodbye":
                            break
                        continue  # Obtain a fresh URL via the API on reconnect.
                    if kind == "error":
                        error = event.get("error", {})
                        raise SlackError("events", error.get("msg") if isinstance(error, dict) else None)
                    if kind in {"huddle_invite", "huddle_invite_cancel", "sh_room_update", "sh_room_leave",
                                "ended_huddle_update"}:
                        self.handle(event)
            finally:
                pings.cancel()
                await asyncio.gather(pings, return_exceptions=True)

    async def run(self):
        delay = 5
        while not self.stopped.is_set():
            try:
                await self.connection()
                delay = 5
            except asyncio.CancelledError:
                raise
            except Exception as error:
                detail = str(error) if isinstance(error, SlackError) else type(error).__name__
                LOG.warning("Slack invitation connection lost error=%s", detail)
                if isinstance(error, SlackError):
                    if error.code in FATAL_AUTH:
                        raise
                    delay = max(delay, error.retry_after)
            finally:
                self.connected = False
            try:
                await asyncio.wait_for(self.stopped.wait(), delay + random.uniform(0, 1))
            except TimeoutError:
                pass
            delay = min(max(delay * 2, 5), 60)

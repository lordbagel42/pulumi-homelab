import asyncio
import logging
import signal
import time
import secrets
import uuid

import aiohttp
from aiohttp import web

from .events import Invitations
from .huddles import active_room, from_invitation, from_message
from .media import MediaServer
from .session import Session
from .slack import Slack, SlackError
from .state import State

LOG = logging.getLogger("huddle-phone")


class Service:
    def __init__(self, config):
        self.config = config
        self.state = State(config.state_path)
        self.media = MediaServer(config, self.health, self.control)
        self.active = self.call_task = None
        self.slack = None
        self.stopped = asyncio.Event()
        self.last_poll = 0
        self.invitations = None
        self.dial_lock = asyncio.Lock()

    def outgoing_status(self, request_id):
        saved = self.state.outgoing(request_id)
        if not saved:
            raise web.HTTPNotFound()
        active = self.active
        if active and active.request_id == request_id:
            return {**saved, "call_id": active.call_id, "phase": active.phase,
                    "display_name": active.caller_name, "connected_at": active.connected_at,
                    "invitation_status": active.invitation_status, "invitation_error": active.invitation_error}
        return {**saved, "phase": saved["status"]}

    async def control(self, request):
        secret = self.config.operator_secret
        if not secret or not secrets.compare_digest(request.headers.get("Authorization", ""), "Bearer " + secret):
            raise web.HTTPForbidden()
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object")
            command = body.get("command")
            if command == "lookup":
                return web.json_response(await self.slack.search_users(body.get("query")))
            if command == "recent":
                return web.json_response({"contacts":self.state.recent_contacts()})
            request_id = str(uuid.UUID(body.get("request_id", "")))
            if command == "status":
                return web.json_response(self.outgoing_status(request_id))
            if command == "cancel":
                if self.active and self.active.request_id == request_id:
                    self.active.stop.set()
                return web.json_response({"ok": True})
            if command != "dial":
                raise ValueError("Unknown command")
            mode = body.get("mode", "ring")
            if mode not in {"ring", "operator"}:
                raise ValueError("Unknown call mode")
            async with self.dial_lock:
                saved = self.state.outgoing(request_id)
                if saved:
                    if saved["user_id"] != body.get("user_id") or saved["mode"] != mode:
                        raise web.HTTPConflict(text="Request ID already used for another call")
                    return web.json_response(self.outgoing_status(request_id))
                if self.config.dry_run:
                    raise web.HTTPConflict(text="Calling disabled in dry run")
                if self.active:
                    raise web.HTTPConflict(text="Phone bridge is occupied")
                user = await self.slack.user(body.get("user_id"))
                # An invitation can reserve the bridge while user lookup awaits.
                if self.active:
                    raise web.HTTPConflict(text="Phone bridge is occupied")
                if not self.state.claim_outgoing(request_id, user["id"], mode, time.time()):
                    raise web.HTTPConflict(text="Call already attempted")
                self.active = Session(self.config, None, self.state, self.slack, self.media, self.verify_active,
                                      target=user["id"], request_id=request_id, external=mode == "operator")
                self.call_task = asyncio.create_task(self.active.run())
                self.call_task.add_done_callback(self.finished)
                return web.json_response(self.outgoing_status(request_id))
        except (ValueError, TypeError, AttributeError):
            return web.json_response({"error": "invalid_request"}, status=400)
        except SlackError as error:
            return web.json_response({"error": error.code}, status=502)

    def health(self):
        feed_ready = not self.config.incoming_invites or bool(self.invitations and self.invitations.connected)
        return {"healthy": time.time() - self.last_poll < max(90, self.config.poll_seconds * 3) and feed_ready,
                "dry_run": self.config.dry_run,
                "invitations_connected": bool(self.invitations and self.invitations.connected),
                "call_phase": self.active.phase if self.active else "idle"}

    def event(self, event):
        invitation = from_invitation(event, team_id=self.config.team_id, enterprise_id=self.config.enterprise_id)
        if invitation and invitation.invited_by != self.slack.user_id:
            LOG.info("Incoming huddle invitation room=%s", invitation.room_id)
            self.accept(invitation)
            return
        active = self.active
        if not active or not active.huddle:
            return
        if (event.get("type") == "huddle_invite_cancel" and active.huddle.invitation_ts
                and event.get("channel_id") == active.huddle.channel_id
                and event.get("call_id", active.huddle.room_id) == active.huddle.room_id
                and active.phase in ("preparing", "ringing") and not active.media.answered.is_set()):
            active.stop.set()
        room = event.get("room", event.get("huddle"))
        if isinstance(room, dict) and room.get("id") == active.huddle.room_id and room.get("has_ended") is True:
            active.stop.set()

    def accept(self, huddle):
        now = time.time()
        self.state.observe(huddle, now)
        if huddle.ended:
            if self.active and self.active.huddle and self.active.huddle.key == huddle.key:
                self.active.stop.set()
            return
        if not self.state.claim(huddle, now, self.config.max_age_seconds):
            return
        if self.config.dry_run:
            self.state.finish(huddle, "dry-run")
            LOG.info("DRY RUN: would call %s for room=%s", self.config.phone_channel, huddle.room_id)
            return
        if self.active:
            self.state.finish(huddle, "busy")
            LOG.info("Skipped room=%s: phone bridge is occupied", huddle.room_id)
            return
        self.active = Session(self.config, huddle, self.state, self.slack, self.media, self.verify_active)
        self.call_task = asyncio.create_task(self.active.run())
        self.call_task.add_done_callback(self.finished)

    def finished(self, task):
        if not task.cancelled() and task.exception():
            LOG.error("Session cleanup error_type=%s", type(task.exception()).__name__)
        self.active = self.call_task = None

    async def verify_active(self, huddle):
        if self.state.is_ended(huddle):
            return False
        try:
            if huddle.invitation_ts:
                return active_room(await self.slack.room_info(huddle), huddle)
            result = await self.slack.history(oldest=huddle.thread_ts, latest=huddle.thread_ts,
                                              inclusive=True, limit=1)
            for message in result.get("messages", []):
                current = from_message(message, team_id=self.config.team_id, channel_id=self.config.channel_id)
                if current and current.key == huddle.key:
                    self.state.observe(current, time.time())
                    return not self.state.is_ended(current)
        except Exception as error:
            LOG.warning("Cannot verify active huddle error=%s", safe_error(error))
        return False

    async def scan(self):
        cursor = None
        now = time.time()
        oldest = f"{now - self.config.max_age_seconds:.6f}"
        # Pin latest while paging, so messages arriving during the scan do not
        # move the window. No channel text is logged or persisted.
        for _ in range(20):
            result = await self.slack.history(oldest=oldest, latest=f"{now:.6f}", cursor=cursor, limit=100)
            for message in result.get("messages", []):
                huddle = from_message(message, team_id=self.config.team_id, channel_id=self.config.channel_id)
                if huddle:
                    self.accept(huddle)
            cursor = result.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                if result.get("has_more"):
                    raise RuntimeError("Incomplete Slack history pagination")
                break
        else:
            raise RuntimeError("Slack history exceeded 20 pages within freshness window")
        if self.active and self.active.huddle and not await self.verify_active(self.active.huddle):
            self.active.stop.set()
        self.last_poll = time.time()
        self.state.prune(now)

    async def polls(self):
        while not self.stopped.is_set():
            delay = self.config.poll_seconds
            try:
                await self.scan()
            except SlackError as error:
                LOG.warning("Huddle scan failed: %s", error)
                delay = max(delay, error.retry_after)
                if error.code in ("invalid_auth", "token_revoked", "account_inactive", "not_authed"):
                    raise
            except Exception as error:
                LOG.warning("Huddle scan failed error=%s", safe_error(error))
            # A long API outage must not leave the phone in a stale call.
            if self.active and time.time() - self.last_poll >= max(90, self.config.poll_seconds * 3):
                self.active.stop.set()
            try:
                await asyncio.wait_for(self.stopped.wait(), delay)
            except TimeoutError:
                pass

    async def run(self):
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, self.stopped.set)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
            self.slack = Slack(self.config, http)
            tasks = []
            try:
                await self.slack.authenticate()
                await self.media.start()
                LOG.info("Watching one Slack channel with user session; dry_run=%s", self.config.dry_run)
                tasks.append(asyncio.create_task(self.polls()))
                if self.config.incoming_invites:
                    self.invitations = Invitations(self.slack, self.event, self.stopped)
                    tasks.append(asyncio.create_task(self.invitations.run()))
                tasks.append(asyncio.create_task(self.stopped.wait()))
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                self.stopped.set()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if self.active:
                    self.active.stop.set()
                    await self.call_task
                await self.media.close()
                self.state.close()


def safe_error(error):
    return str(error) if isinstance(error, SlackError) else type(error).__name__

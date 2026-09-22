import asyncio
import logging
import re
import time
import uuid

from .ami import AMI
from .browser import ChimeError, ChimeRuntime
from .media import MediaCall
from .slack import SlackError

LOG = logging.getLogger("huddle-phone")


class Session:
    def __init__(self, config, huddle, state, slack, media, verify_active, *, target=None,
                 request_id=None, external=False):
        self.config, self.huddle, self.state = config, huddle, state
        self.slack, self.media_server = slack, media
        self.verify_active = verify_active
        self.call_id = str(uuid.uuid4())
        self.target, self.request_id, self.external = target, request_id, external
        self.credentials = None
        self.media = MediaCall(self.call_id)
        self.ami = AMI(config)
        self.runtime = ChimeRuntime(config)
        self.stop = asyncio.Event()
        self.phase = "preparing"
        self.invitation_status = "not-requested"
        self.invitation_error = None
        self.caller_name = "Slack huddle"
        self.connected_at = None

    async def _run(self):
        if not self.external:
            await self.ami.connect()
        self.media_server.active = self.media
        await self.runtime.prepare(self.call_id, self.media.token)
        contact = self.target or (self.huddle.invited_by if self.huddle else "")
        if contact and not self.stop.is_set():
            try:
                # Caller-ID enrichment must not prevent or indefinitely delay
                # an otherwise valid incoming call when Slack is unavailable.
                async with asyncio.timeout(2):
                    self.caller_name = await self.slack.caller_name(contact)
            except Exception as error:
                LOG.warning("Slack caller name unavailable error_type=%s", type(error).__name__)
        if self.stop.is_set() or (self.huddle and not await self.verify_active(self.huddle)) or self.stop.is_set():
            return
        self.phase = "awaiting-handset" if self.external else "ringing"
        phone_done = asyncio.Event()
        if not self.external:
            await self.ami.originate(self.call_id, caller_name=self.caller_name)
            phone_done = self.ami.calls[self.call_id]
            LOG.info("Ringing phone room=%s outgoing=%s", self.huddle.room_id if self.huddle else "pending", bool(self.target))
        waits = [asyncio.create_task(self.media.answered.wait()),
                 asyncio.create_task(phone_done.wait()),
                 asyncio.create_task(self.stop.wait()), asyncio.create_task(self.media.done.wait())]
        try:
            await asyncio.wait(waits, timeout=self.config.ring_seconds + 5,
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in waits:
                task.cancel()
            await asyncio.gather(*waits, return_exceptions=True)
        if (not self.media.answered.is_set() or self.stop.is_set() or self.media.done.is_set()
                or phone_done.is_set() or (self.huddle and not await self.verify_active(self.huddle))
                or self.stop.is_set()):
            return
        self.phase = "joining"
        if self.target:
            self.huddle, self.credentials = await self.slack.start_huddle(self.target)
        else:
            self.credentials = await self.slack.join(self.huddle)
        if self.stop.is_set() or self.media.done.is_set():
            return
        await self.runtime.join(self.credentials)
        if self.stop.is_set() or self.media.done.is_set():
            return
        self.phase = "connected"
        self.connected_at = time.time()
        # This hook is after handset answer and successful media join. Missed,
        # rejected, cancelled and failed calls never enter recent contacts.
        contact = self.target or (self.huddle.invited_by if self.huddle else "")
        if (isinstance(contact,str) and re.fullmatch(r"[UW][A-Z0-9]+",contact)
                and contact != getattr(self.slack,"user_id",None)):
            try:
                self.state.record_contact(contact,self.connected_at)
            except Exception as error:
                # Contact bookkeeping must never disconnect working audio.
                LOG.warning("Could not save recent huddle contact error_type=%s",type(error).__name__)
        LOG.info("Handset joined native Chime huddle room=%s", self.huddle.room_id)
        if self.target:
            self.invitation_status = "checking"
            try:
                self.invitation_status = await self.slack.invite(self.huddle, self.target)
                LOG.info("Outgoing invitation room=%s status=%s", self.huddle.room_id, self.invitation_status)
            except Exception as error:
                # A notification is separate from the established media call.
                # Even a permission/rate-limit/network failure must not eject
                # the handset or someone who has already joined the huddle.
                self.invitation_status = "failed"
                self.invitation_error = error.code if isinstance(error, SlackError) else type(error).__name__
                LOG.warning("Invitation failed; keeping Chime connected room=%s error=%s",
                            self.huddle.room_id, self.invitation_error)
        while not self.stop.is_set() and not self.media.done.is_set() and not self.ami.failure.is_set():
            if phone_done.is_set() or not await self.runtime.healthy():
                return
            try:
                await asyncio.wait_for(self.stop.wait(), 1)
            except TimeoutError:
                pass

    async def run(self):
        status = "finished"
        work = asyncio.create_task(self._run())
        stopped = asyncio.create_task(self.stop.wait())
        disconnected = asyncio.create_task(self.media.done.wait())
        try:
            done, _ = await asyncio.wait([work, stopped, disconnected], timeout=self.config.max_call_seconds,
                                         return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                work.result()
            elif not self.stop.is_set() and not self.media.done.is_set():
                status = "time-limit"
        except asyncio.CancelledError:
            status = "interrupted"
            raise
        except Exception as error:
            status = "failed"
            detail = str(error) if isinstance(error, (SlackError, ChimeError)) else type(error).__name__
            LOG.error("Call failed room=%s phase=%s error=%s", self.huddle.room_id if self.huddle else "pending", self.phase, detail)
        finally:
            self.phase = "closing"
            self.stop.set()
            work.cancel()
            stopped.cancel()
            disconnected.cancel()
            await asyncio.gather(work, stopped, disconnected, return_exceptions=True)
            # Each cleanup is independent so a failed AMI hangup cannot keep
            # the Chime connection alive (and vice versa).
            for cleanup in (lambda: self.ami.hangup(self.call_id), self.runtime.close,
                            self.media.close, self.ami.close):
                try:
                    await cleanup()
                except Exception as error:
                    LOG.error("Call cleanup error_type=%s", type(error).__name__)
            self.media_server.active = None
            if self.target and self.huddle and self.credentials:
                try:
                    await self.slack.leave(self.huddle, self.credentials)
                except Exception as error:
                    LOG.warning("Slack leave error=%s", str(error) if isinstance(error, SlackError) else type(error).__name__)
            if self.request_id:
                self.state.finish_outgoing(self.request_id, status)
            else:
                self.state.finish(self.huddle, status)
            LOG.info("Call closed room=%s status=%s phone_rx_bytes=%s phone_tx_bytes=%s",
                     self.huddle.room_id if self.huddle else "pending", status, self.media.rx_bytes, self.media.tx_bytes)

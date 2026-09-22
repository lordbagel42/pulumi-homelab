import asyncio
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from huddle_phone.huddles import Huddle
from huddle_phone.session import Session


class SessionTests(unittest.IsolatedAsyncioTestCase):
    def make_session(self):
        config = SimpleNamespace(ring_seconds=5, max_call_seconds=60)
        state = Mock()
        slack = SimpleNamespace(caller_name=AsyncMock(return_value="Slack display name"), join=AsyncMock(return_value={"meeting": {}, "attendee": {}}))
        media = SimpleNamespace(active=None)
        huddle = Huddle("T123", "C123", "R123", 1000, False, "1000.000001")
        session = Session(config, huddle, state, slack, media, AsyncMock(return_value=True))
        session.ami = SimpleNamespace(connect=AsyncMock(), originate=AsyncMock(), hangup=AsyncMock(),
                                       close=AsyncMock(), calls={}, failure=asyncio.Event())
        session.runtime = SimpleNamespace(prepare=AsyncMock(), join=AsyncMock(),
                                          healthy=AsyncMock(return_value=True), close=AsyncMock())
        return session

    async def test_rejected_handset_never_joins_huddle(self):
        session = self.make_session()
        async def ring(call_id, *, caller_name):
            session.ami.calls[call_id] = asyncio.Event()
            session.ami.calls[call_id].set()  # busy / no answer / rejected
        session.ami.originate.side_effect = ring
        await session.run()
        session.slack.join.assert_not_awaited()
        session.runtime.join.assert_not_awaited()
        session.runtime.close.assert_awaited_once()
        session.ami.hangup.assert_awaited_once_with(session.call_id)
        session.state.record_contact.assert_not_called()

    async def test_slack_join_is_after_answer_and_disconnect_cleans_up(self):
        session = self.make_session()
        async def ring(call_id, *, caller_name):
            session.slack.join.assert_not_awaited()
            session.ami.calls[call_id] = asyncio.Event()
            session.media.answered.set()
        async def joined(_):
            self.assertTrue(session.media.answered.is_set())
            session.media.done.set()  # handset disconnects
        session.ami.originate.side_effect = ring
        session.runtime.join.side_effect = joined
        await session.run()
        session.slack.join.assert_awaited_once_with(session.huddle)
        session.runtime.join.assert_awaited_once()
        session.runtime.close.assert_awaited_once()
        self.assertIsNone(session.media_server.active)

    async def test_huddle_ending_before_ring_never_calls_phone(self):
        session = self.make_session()
        session.verify_active.return_value = False
        await session.run()
        session.ami.originate.assert_not_awaited()
        session.slack.join.assert_not_awaited()
        session.state.record_contact.assert_not_called()

    async def test_invitation_cancelled_during_room_check_never_rings(self):
        session = self.make_session()
        async def verify(_):
            session.stop.set()
            return True
        session.verify_active.side_effect = verify
        await session.run()
        session.ami.originate.assert_not_awaited()

    async def test_incoming_call_uses_inviter_name_before_ringing(self):
        session = self.make_session()
        session.huddle = replace(session.huddle, invited_by="U1234567890", invitation_ts="1000.1")
        async def ring(call_id, *, caller_name):
            self.assertEqual("Slack display name", caller_name)
            session.ami.calls[call_id] = asyncio.Event()
            session.ami.calls[call_id].set()
        session.ami.originate.side_effect = ring
        await session.run()
        session.slack.caller_name.assert_awaited_once_with("U1234567890")
        session.ami.originate.assert_awaited_once()

    async def test_failed_name_lookup_still_rings_with_fallback(self):
        session = self.make_session()
        session.huddle = replace(session.huddle, invited_by="U1234567890", invitation_ts="1000.1")
        session.slack.caller_name.side_effect = TimeoutError()
        async def ring(call_id, *, caller_name):
            self.assertEqual("Slack huddle", caller_name)
            session.ami.calls[call_id] = asyncio.Event()
            session.ami.calls[call_id].set()
        session.ami.originate.side_effect = ring
        with self.assertLogs("huddle-phone", level="WARNING"):
            await session.run()
        session.ami.originate.assert_awaited_once()

    async def test_invitation_cancelled_during_name_lookup_never_rings(self):
        session = self.make_session()
        session.huddle = replace(session.huddle, invited_by="U1234567890", invitation_ts="1000.1")
        async def lookup(_):
            session.stop.set()
            return "Cancelled caller"
        session.slack.caller_name.side_effect = lookup
        await session.run()
        session.ami.originate.assert_not_awaited()

    async def test_huddle_end_interrupts_ringing_without_slack_join(self):
        session = self.make_session()
        async def ring(call_id, *, caller_name):
            session.ami.calls[call_id] = asyncio.Event()
            session.stop.set()
        session.ami.originate.side_effect = ring
        await session.run()
        session.slack.join.assert_not_awaited()
        session.ami.hangup.assert_awaited_once()
        session.runtime.close.assert_awaited_once()

    async def test_sdk_failure_still_hangs_up_phone(self):
        session = self.make_session()
        async def ring(call_id, *, caller_name):
            session.ami.calls[call_id] = asyncio.Event()
            session.media.answered.set()
        session.ami.originate.side_effect = ring
        session.runtime.join.side_effect = RuntimeError("synthetic failure")
        with self.assertLogs("huddle-phone", level="ERROR"):
            await session.run()
        session.ami.hangup.assert_awaited_once()
        session.runtime.close.assert_awaited_once()
        session.state.finish.assert_called_with(session.huddle, "failed")
        session.state.record_contact.assert_not_called()

    async def test_connected_incoming_inviter_is_recorded_after_media_join(self):
        session = self.make_session()
        session.huddle = replace(session.huddle,invited_by="U123",invitation_ts="1000.1")
        session.slack.user_id = "UOWNER"
        async def ring(call_id, *, caller_name):
            session.state.record_contact.assert_not_called()
            session.ami.calls[call_id] = asyncio.Event()
            session.media.answered.set()
        async def joined(_):
            session.state.record_contact.assert_not_called()
        session.ami.originate.side_effect = ring
        session.runtime.join.side_effect = joined
        session.runtime.healthy.return_value = False
        await session.run()
        session.state.record_contact.assert_called_once()
        self.assertEqual("U123",session.state.record_contact.call_args.args[0])
        self.assertGreater(session.state.record_contact.call_args.args[1],0)

    async def test_connected_outgoing_target_is_recorded(self):
        session = self.make_session()
        session.target = "U456"
        session.slack.user_id = "UOWNER"
        session.slack.start_huddle = AsyncMock(return_value=(session.huddle,{"meeting":{},"attendee":{}}))
        session.slack.invite = AsyncMock()
        session.slack.leave = AsyncMock()
        async def ring(call_id, *, caller_name):
            session.ami.calls[call_id] = asyncio.Event()
            session.media.answered.set()
        session.ami.originate.side_effect = ring
        session.runtime.healthy.return_value = False
        await session.run()
        session.slack.invite.assert_awaited_once()
        self.assertEqual("U456",session.state.record_contact.call_args.args[0])

    async def test_own_invitation_is_not_a_recent_person(self):
        session = self.make_session()
        session.huddle = replace(session.huddle,invited_by="UOWNER",invitation_ts="1000.1")
        session.slack.user_id = "UOWNER"
        async def ring(call_id, *, caller_name):
            session.ami.calls[call_id] = asyncio.Event()
            session.media.answered.set()
        session.ami.originate.side_effect = ring
        session.runtime.healthy.return_value = False
        await session.run()
        session.state.record_contact.assert_not_called()

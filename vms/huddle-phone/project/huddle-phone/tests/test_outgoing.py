import asyncio
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import uuid

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from huddle_phone.app import Service
from huddle_phone.huddles import Huddle
from huddle_phone.session import Session
from huddle_phone.slack import Slack, SlackError, user_summary
from huddle_phone.state import State

USER = "U1234567890"
MEMBER = {"id": USER, "name": "Test Person", "username": "test.person"}
HUDDLE = Huddle("T123", "D123", "R123", time.time(), False, "", "outgoing")
CREDS = {"meeting": {}, "attendee": {"AttendeeId": "test-attendee"}}


class OutgoingSessionTests(unittest.IsolatedAsyncioTestCase):
    def session(self, external=False):
        config = SimpleNamespace(ring_seconds=5, max_call_seconds=60)
        slack = SimpleNamespace(caller_name=AsyncMock(return_value="Slack display name"), start_huddle=AsyncMock(return_value=(HUDDLE, CREDS)),
                                invite=AsyncMock(), leave=AsyncMock())
        session = Session(config, None, Mock(), slack, SimpleNamespace(active=None), AsyncMock(),
                          target=USER, request_id=str(uuid.uuid4()), external=external)
        session.ami = SimpleNamespace(connect=AsyncMock(), originate=AsyncMock(), hangup=AsyncMock(),
                                      close=AsyncMock(), calls={}, failure=asyncio.Event())
        session.runtime = SimpleNamespace(prepare=AsyncMock(), join=AsyncMock(),
                                          healthy=AsyncMock(return_value=True), close=AsyncMock())
        return session

    async def test_declined_phone_never_opens_dm_or_invites(self):
        s = self.session()
        async def originate(call_id, *, caller_name):
            s.ami.calls[call_id] = asyncio.Event()
            s.ami.calls[call_id].set()
        s.ami.originate.side_effect = originate
        await s.run()
        s.slack.start_huddle.assert_not_awaited()
        s.slack.invite.assert_not_awaited()

    async def test_operator_waits_for_audio_then_joins_and_invites_once(self):
        s = self.session(external=True)
        async def invite(*args):
            s.runtime.join.assert_awaited_once_with(CREDS)
            self.assertTrue(s.media.answered.is_set())
            s.media.done.set()
        s.slack.invite.side_effect = invite
        task = asyncio.create_task(s.run())
        async with asyncio.timeout(2):
            while s.phase != "awaiting-handset":
                await asyncio.sleep(.001)
        s.slack.start_huddle.assert_not_awaited()
        s.ami.originate.assert_not_awaited()
        s.media.answered.set()
        await task
        s.slack.start_huddle.assert_awaited_once_with(USER)
        s.slack.invite.assert_awaited_once_with(HUDDLE, USER)
        s.slack.leave.assert_awaited_once_with(HUDDLE, CREDS)

    async def test_hangup_during_chime_join_does_not_send_invitation(self):
        s = self.session(external=True)
        s.media.answered.set()
        async def joined(_):
            s.media.done.set()
        s.runtime.join.side_effect = joined
        await s.run()
        s.slack.invite.assert_not_awaited()
        s.slack.leave.assert_awaited_once()

    async def test_sdk_failure_releases_created_slack_room(self):
        s = self.session(external=True)
        s.media.answered.set()
        s.runtime.join.side_effect = RuntimeError("synthetic")
        with self.assertLogs("huddle-phone", level="ERROR"):
            await s.run()
        s.slack.invite.assert_not_awaited()
        s.slack.leave.assert_awaited_once()
        s.state.finish_outgoing.assert_called_once_with(s.request_id, "failed")

    async def test_notification_rejection_does_not_disconnect_answered_huddle(self):
        for error in (SlackError("rooms.notifyMember", "permission_denied"),
                      SlackError("rooms.notifyMember", "ratelimited"), TimeoutError()):
            with self.subTest(error=type(error).__name__):
                s = self.session(external=True)
                s.media.answered.set()
                s.slack.invite.side_effect = error
                with self.assertLogs("huddle-phone", level="WARNING"):
                    task = asyncio.create_task(s.run())
                    try:
                        async with asyncio.timeout(2):
                            while s.invitation_status != "failed":
                                await asyncio.sleep(.001)
                        self.assertEqual(s.phase, "connected")
                        self.assertFalse(task.done())
                        s.runtime.close.assert_not_awaited()
                        s.slack.leave.assert_not_awaited()
                        self.assertIs(s.media_server.active, s.media)
                    finally:
                        s.stop.set()
                        await task
                s.runtime.close.assert_awaited_once()
                s.slack.leave.assert_awaited_once()
                s.state.finish_outgoing.assert_called_once_with(s.request_id, "finished")

    async def test_phone_hangup_cancels_invitation_lookup_before_notification(self):
        s = self.session(external=True)
        lookup_started = asyncio.Event()
        slack = Slack(SimpleNamespace(), None)
        slack.caller_name = s.slack.caller_name
        slack.start_huddle = s.slack.start_huddle
        slack.leave = s.slack.leave
        slack.api = AsyncMock()
        async def room_info(_):
            lookup_started.set()
            await asyncio.Event().wait()
        slack.room_info = room_info
        s.slack = slack
        s.media.answered.set()
        task = asyncio.create_task(s.run())
        await asyncio.wait_for(lookup_started.wait(), 2)
        s.media.done.set()
        await asyncio.wait_for(task, 2)
        slack.api.assert_not_awaited()
        slack.leave.assert_awaited_once()


class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.config = SimpleNamespace(state_path=":memory:", operator_secret="x" * 32, dry_run=False)
        self.service = Service(self.config)
        self.service.slack = SimpleNamespace(user=AsyncMock(return_value=MEMBER), search_users=AsyncMock())
        app = web.Application()
        app.router.add_post("/control", self.service.control)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.request_id = str(uuid.uuid4())
        self.body = {"command": "dial", "request_id": self.request_id, "user_id": USER, "mode": "operator"}
        self.headers = {"Authorization": "Bearer " + self.config.operator_secret}

    async def asyncTearDown(self):
        if self.service.call_task:
            self.service.call_task.cancel()
            await asyncio.gather(self.service.call_task, return_exceptions=True)
        await self.client.close()
        self.service.state.close()

    async def test_requests_require_secret_before_lookup_or_mutations(self):
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            response = await self.client.post("/control", json=self.body, headers=headers)
            self.assertEqual(response.status, 403)
        self.service.slack.user.assert_not_awaited()

    async def test_duplicate_request_never_rings_twice_and_conflicting_reuse_fails(self):
        async def pending():
            await asyncio.Event().wait()
        session = SimpleNamespace(run=pending, call_id=str(uuid.uuid4()), request_id=self.request_id,
                                  phase="preparing", stop=asyncio.Event(),
                                  invitation_status="not-requested", invitation_error=None, caller_name="Slack display name", connected_at=None)
        with patch("huddle_phone.app.Session", return_value=session) as constructor:
            for _ in range(2):
                response = await self.client.post("/control", json=self.body, headers=self.headers)
                self.assertEqual(response.status, 200)
                self.assertEqual((await response.json())["call_id"], session.call_id)
            constructor.assert_called_once()
            response = await self.client.post("/control", json={**self.body, "user_id": "U9876543210"}, headers=self.headers)
            self.assertEqual(response.status, 409)

    async def test_incoming_invite_can_win_during_user_lookup(self):
        async def lookup(_):
            self.service.active = object()
            return MEMBER
        self.service.slack.user.side_effect = lookup
        response = await self.client.post("/control", json=self.body, headers=self.headers)
        self.assertEqual(response.status, 409)
        self.assertIsNone(self.service.state.outgoing(self.request_id))

    async def test_dry_run_and_bad_request_cannot_dial(self):
        self.config.dry_run = True
        response = await self.client.post("/control", json=self.body, headers=self.headers)
        self.assertEqual(response.status, 409)
        response = await self.client.post("/control", json={**self.body, "request_id": "bad"}, headers=self.headers)
        self.assertEqual(response.status, 400)
        self.service.slack.user.assert_not_awaited()


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_dm_invitation_or_participant_is_not_notified_again(self):
        for state in ({"participants": [USER]}, {"pending_invitees": {USER: "pending"}},
                      {"last_invite_status_by_user": {USER: "accepted"}},
                      {"last_invite_status_by_user": {USER: "joining_soon"}}):
            with self.subTest(state=state):
                slack = Slack(SimpleNamespace(), None)
                slack.api = AsyncMock(return_value={"room": {"id": "R123", "has_ended": False, **state}})
                result = await slack.invite(HUDDLE, USER)
                self.assertIn(result, {"already-present", "already-invited"})
                slack.api.assert_awaited_once_with("screenhero.rooms.info", room="R123")

    async def test_recipient_without_existing_invitation_is_notified_once(self):
        slack = Slack(SimpleNamespace(), None)
        slack.api = AsyncMock(side_effect=[{"room": {"id": "R123", "has_ended": False,
                 "participants": [], "pending_invitees": {}, "last_invite_status_by_user": {}}}, {"ok": True}])
        self.assertEqual(await slack.invite(HUDDLE, USER), "sent")
        self.assertEqual(slack.api.await_args_list[-1].args, ("rooms.notifyMember",))
        self.assertEqual(slack.api.await_args_list[-1].kwargs, {"channel_id": "D123", "user_id": USER})

    async def test_ended_or_wrong_room_does_not_receive_invitation(self):
        slack = Slack(SimpleNamespace(), None)
        slack.api = AsyncMock(return_value={"room": {"id": "R123", "has_ended": True}})
        self.assertEqual(await slack.invite(HUDDLE, USER), "room-ended")
        self.assertEqual(slack.api.await_count, 1)
        slack.api.return_value = {"room": {"id": "R999", "has_ended": False}}
        with self.assertRaises(SlackError):
            await slack.invite(HUDDLE, USER)

    async def test_workspace_without_explicit_leave_uses_chime_disconnect(self):
        slack = Slack(SimpleNamespace(), None)
        slack.api = AsyncMock(side_effect=SlackError("rooms.leave", "feature_not_enabled"))
        await slack.leave(HUDDLE, CREDS)
        await slack.leave(HUDDLE, CREDS)
        self.assertEqual(slack.api.await_count, 1)
        self.assertFalse(slack.leave_supported)

    async def test_leave_uses_slacks_user_initiated_reason(self):
        slack = Slack(SimpleNamespace(), None)
        slack.api = AsyncMock()
        await slack.leave(HUDDLE, CREDS)
        slack.api.assert_awaited_once_with("rooms.leave", channel_id="D123", call_id="R123",
                                           attendee_id="test-attendee", reason="user_initiated")

    async def test_start_uses_only_target_dm_and_current_native_join_protocol(self):
        slack = Slack(SimpleNamespace(team_id="T123", region="us-east-2"), None)
        response = {"call": {"call_id": "R123", "free_willy": {"meeting": {"MeetingId": "test",
                     "MediaPlacement": {"AudioHostUrl": "test", "SignalingUrl": "test", "TurnControlUrl": "test"}},
                     "attendee": {"AttendeeId": "test", "JoinToken": "test"}}}}
        slack.api = AsyncMock(side_effect=[{"channel": {"id": "D123"}}, response])
        huddle, credentials = await slack.start_huddle(USER)
        self.assertEqual(huddle.channel_id, "D123")
        self.assertEqual(huddle.room_id, "R123")
        self.assertEqual(slack.api.await_args_list[0].kwargs, {"users": USER})
        self.assertEqual(slack.api.await_args_list[1].kwargs,
                         {"channel_id": "D123", "regions": "us-east-2", "multidevice": True})

    async def test_invalid_or_nonhuman_recipient_rejected(self):
        slack = Slack(SimpleNamespace(), None)
        slack.user_id = USER
        slack.api = AsyncMock(return_value={"user": {"id": USER}})
        for user_id in (None, "C1234567890", "U123\n", USER):
            with self.assertRaises(ValueError):
                await slack.user(user_id)
        for flag in ("deleted", "is_bot", "is_app_user", "is_invited_user", "is_profile_only"):
            self.assertIsNone(user_summary({"id": USER, flag: True}))

    def test_requests_remain_claimed_after_process_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = folder + "/state.db"
            state = State(path)
            self.assertTrue(state.claim_outgoing("request", USER, "ring", time.time()))
            state.close()
            state = State(path)
            self.assertFalse(state.claim_outgoing("request", USER, "ring", time.time()))
            self.assertEqual(state.outgoing("request")["status"], "interrupted")
            state.close()

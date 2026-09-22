import asyncio
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from huddle_phone.app import Service
from huddle_phone.events import Invitations, socket_url
from huddle_phone.huddles import active_room, from_invitation
from huddle_phone.slack import SlackError
from huddle_phone.state import State


def invitation(timestamp="1000.000700", **changes):
    # Shape observed on the authenticated Enterprise user socket. No credentials.
    return {"type": "huddle_invite", "team_id": "E123", "channel_id": "D123", "call_id": "R123",
            "sender_user_id": "U456", "event_ts": timestamp, "free_willy": {"secret": "never-retain"}, **changes}


def parse(event):
    return from_invitation(event, team_id="T123", enterprise_id="E123")


class InvitationTests(unittest.TestCase):
    def test_direct_invite_uses_enterprise_identity_and_invite_freshness(self):
        huddle = parse(invitation())
        self.assertEqual(huddle.channel_id, "D123")
        self.assertEqual(huddle.team_id, "T123")
        self.assertEqual(huddle.invited_by, "U456")
        self.assertNotIn("never-retain", repr(huddle))
        self.assertTrue(active_room({"id": "R123", "call_family": "huddle", "channels": ["D123"],
                                     "has_ended": False, "date_start": 1, "date_end": 0}, huddle))
        state = State(":memory:")
        try:
            self.assertTrue(state.claim(huddle, 1001, 120))
            state.finish(huddle, "finished")
            self.assertFalse(state.claim(huddle, 1002, 120))
            self.assertTrue(state.claim(parse(invitation("1010.000700")), 1011, 120))
        finally:
            state.close()

    def test_malformed_foreign_or_nontargeted_events_cannot_ring(self):
        for changes in ({"type": "huddle_start"}, {"type": "user_huddle_changed"}, {"team_id": "E999"},
                        {"team_id": None}, {"channel_id": "C123\n"}, {"call_id": "bad"},
                        {"sender_user_id": None}, {"event_ts": "nan"}, {"event_ts": None}):
            self.assertIsNone(parse(invitation(**changes)), changes)

    def test_ended_wrong_or_incomplete_room_cannot_join(self):
        huddle = parse(invitation())
        for room in (None, {}, {"id": "R999"}, {"id": "R123", "call_family": "huddle", "has_ended": False,
                                                "channels": None}):
            self.assertFalse(active_room(room, huddle))
        self.assertFalse(active_room({"id": "R123", "call_family": "huddle", "channels": ["D123"],
                                      "has_ended": True, "date_end": 1001}, huddle))

    def test_socket_credentials_only_go_to_slack_over_tls(self):
        self.assertEqual(socket_url("wss://wss-primary.slack.com/?test=1&token=old", "xoxc-test"),
                         "wss://wss-primary.slack.com/?test=1&token=xoxc-test")
        for url in (None, "ws://wss-primary.slack.com/", "wss://slack.com.attacker.test/",
                    "wss://x@wss-primary.slack.com/", "wss://wss-primary.slack.com:bad/"):
            with self.assertRaises(SlackError):
                socket_url(url, "private-token")


class InvitationServiceTests(unittest.IsolatedAsyncioTestCase):
    def service(self):
        config = SimpleNamespace(state_path=":memory:", dry_run=True, max_age_seconds=120, team_id="T123",
                                 enterprise_id="E123", phone_channel="SCCP/6738", incoming_invites=True,
                                 poll_seconds=5)
        service = Service(config)
        service.slack = SimpleNamespace(user_id="U123", room_info=AsyncMock())
        self.addCleanup(service.state.close)
        return service

    async def test_invite_from_dm_uses_same_durable_call_policy(self):
        service = self.service()
        event = invitation(f"{time.time():.6f}")
        with self.assertLogs("huddle-phone", level="INFO") as logs:
            service.event(event)
            service.event(event)
        self.assertEqual(sum("DRY RUN" in line for line in logs.output), 1)
        self.assertNotIn("never-retain", str(logs.output))

    async def test_cancel_stops_ringing_but_not_an_answered_call(self):
        service = self.service()
        active = SimpleNamespace(huddle=parse(invitation()), phase="ringing", stop=asyncio.Event(),
                                  media=SimpleNamespace(answered=asyncio.Event()))
        service.active = active
        event = {"type": "huddle_invite_cancel", "channel_id": "D123", "call_id": "R123"}
        service.event({**event, "call_id": "R999"})
        self.assertFalse(active.stop.is_set())
        service.event(event)
        self.assertTrue(active.stop.is_set())
        active.stop.clear()
        active.media.answered.set()
        service.event(event)
        self.assertFalse(active.stop.is_set())

    async def test_invited_room_is_checked_without_reading_dm_text(self):
        service = self.service()
        service.slack.room_info.return_value = {
            "id": "R123", "channels": ["D123"], "has_ended": False, "date_end": 0, "call_family": "huddle"}
        self.assertTrue(await service.verify_active(parse(invitation())))
        service.slack.room_info.return_value["has_ended"] = True
        self.assertFalse(await service.verify_active(parse(invitation())))

    async def test_unavailable_socket_is_visible_in_health(self):
        service = self.service()
        service.last_poll = time.time()
        service.invitations = SimpleNamespace(connected=False)
        self.assertFalse(service.health()["healthy"])
        service.invitations.connected = True
        self.assertTrue(service.health()["healthy"])

    async def test_socket_errors_do_not_disclose_credential_url(self):
        service = self.service()
        feed = Invitations(service.slack, service.event, service.stopped)
        async def failure():
            service.stopped.set()
            raise RuntimeError("wss://example.invalid?token=private-token")
        feed.connection = failure
        with self.assertLogs("huddle-phone", level="WARNING") as logs:
            await feed.run()
        self.assertNotIn("private-token", str(logs.output))

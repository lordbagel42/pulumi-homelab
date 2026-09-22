import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from huddle_phone.slack import Slack, SlackError, chime_credentials


def join_response():
    return {"ok": True, "call": {"call_id": "R123", "free_willy": {
        "meeting": {"MeetingId": "11111111-2222-3333-4444-555555555555", "MediaRegion": "us-east-2",
                    "MediaPlacement": {"AudioHostUrl": "audio.example.test:3478",
                                       "SignalingUrl": "wss://signal.example.test/control/meeting",
                                       "TurnControlUrl": "https://turn.example.test/v2/turn_sessions"}},
        "attendee": {"AttendeeId": "fake-attendee", "ExternalUserId": "T123-R123-U123", "JoinToken": "fake-token"},
    }}}


class CredentialsTests(unittest.TestCase):
    def test_maps_the_observed_slack_response_to_chime_sdk_objects(self):
        result = chime_credentials(join_response(), "R123")
        self.assertEqual(result["attendee"]["JoinToken"], "fake-token")
        self.assertEqual(set(result), {"meeting", "attendee"})

    def test_wrong_room_new_backend_and_incomplete_credentials_fail_closed(self):
        cases = []
        wrong_room = join_response()
        wrong_room["call"]["call_id"] = "R999"
        cases.append(wrong_room)
        cases.append({"call": {"call_id": "R123", "another_backend": {}}})
        missing_token = join_response()
        del missing_token["call"]["free_willy"]["attendee"]["JoinToken"]
        cases.append(missing_token)
        missing_placement = join_response()
        del missing_placement["call"]["free_willy"]["meeting"]["MediaPlacement"]
        cases.append(missing_placement)
        for case in cases:
            with self.assertRaises(SlackError):
                chime_credentials(case, "R123")


class FakeResponse:
    def __init__(self, payload, status=200, headers=None):
        self.payload, self.status, self.headers = payload, status, headers or {}
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def iter_chunked(self, _):
        yield json.dumps(self.payload).encode()


class FakeHTTP:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class SlackTests(unittest.IsolatedAsyncioTestCase):
    def config(self):
        return SimpleNamespace(client_token="xoxc-test", cookie="d=xoxd-test", workspace="example",
                               channel_id="C123", region="us-east-2")

    async def test_caller_uses_slack_display_name_then_real_name_then_username(self):
        user = {"id": "U1234567890", "name": "login", "real_name": "Account Name",
                "profile": {"display_name": "  Slack   Nickname  ", "real_name": "Full Name"}}
        slack = Slack(self.config(), None)
        slack.api = AsyncMock(return_value={"user": user})
        self.assertEqual("Slack Nickname", await slack.caller_name(user["id"]))
        user["profile"]["display_name"] = " "
        self.assertEqual("Full Name", await slack.caller_name(user["id"]))
        user["profile"] = None
        self.assertEqual("Account Name", await slack.caller_name(user["id"]))
        del user["real_name"]
        self.assertEqual("login", await slack.caller_name(user["id"]))
        slack.api.assert_awaited_with("users.info", user=user["id"])

    async def test_caller_lookup_cannot_use_another_users_profile(self):
        slack = Slack(self.config(), None)
        slack.api = AsyncMock(return_value={"user": {"id": "U9999999999", "name": "Wrong person"}})
        with self.assertRaises(SlackError):
            await slack.caller_name("U1234567890")

    async def test_private_join_uses_user_cookie_and_does_not_retry(self):
        http = FakeHTTP(FakeResponse(join_response()))
        slack = Slack(self.config(), http)
        result = await slack.join(SimpleNamespace(channel_id="C123", room_id="R123"))
        self.assertIn("attendee", result)
        self.assertEqual(len(http.calls), 1)
        url, request = http.calls[0]
        self.assertEqual(url, "https://example.slack.com/api/rooms.join")
        self.assertEqual(request["headers"]["Cookie"], "d=xoxd-test")
        self.assertFalse(request["allow_redirects"])
        self.assertTrue(request["data"].is_multipart)

    async def test_rate_limit_is_honored_without_another_request(self):
        http = FakeHTTP(FakeResponse({}, 429, {"Retry-After": "120"}))
        slack = Slack(self.config(), http)
        for _ in range(2):
            with self.assertRaises(SlackError) as result:
                await slack.history(limit=100)
            self.assertGreater(result.exception.retry_after, 119)
        self.assertEqual(len(http.calls), 1)

    async def test_errors_cannot_echo_tokens_into_logs(self):
        http = FakeHTTP(FakeResponse({"ok": False, "error": "xoxc-sensitive\nresponse dump"}))
        with self.assertRaises(SlackError) as result:
            await Slack(self.config(), http).history(limit=100)
        self.assertNotIn("sensitive", str(result.exception))

    async def test_enterprise_session_verifies_the_channel_workspace(self):
        config = self.config()
        config.team_id, config.enterprise_id = "T123", "E123"
        slack = Slack(config, None)
        slack.api = AsyncMock(side_effect=[
            {"team_id": "E123", "enterprise_id": "E123", "user_id": "U123"},
            {"channel": {"context_team_id": "T123", "is_member": True, "is_archived": False}},
        ])
        await slack.authenticate()
        self.assertEqual(slack.user_id, "U123")
        slack.api = AsyncMock(side_effect=[
            {"team_id": "E123", "enterprise_id": "E123"},
            {"channel": {"context_team_id": "T999", "is_member": True}},
        ])
        with self.assertRaisesRegex(ValueError, "channel's workspace"):
            await slack.authenticate()
        slack.api = AsyncMock(return_value={"team_id": "E999", "enterprise_id": "E999"})
        with self.assertRaisesRegex(ValueError, "organization"):
            await slack.authenticate()

    async def test_enterprise_requests_include_workspace_context(self):
        config = self.config()
        config.team_id, config.enterprise_id, config.workspace = "T123", "E123", "example.enterprise"
        http = FakeHTTP(FakeResponse({"ok": True, "messages": []}))
        await Slack(config, http).history(limit=1)
        url, request = http.calls[0]
        self.assertEqual(url, "https://example.enterprise.slack.com/api/conversations.history")
        fields = {headers["name"]: value for headers, _, value in request["data"]._fields}
        self.assertEqual(fields["team_id"], "T123")

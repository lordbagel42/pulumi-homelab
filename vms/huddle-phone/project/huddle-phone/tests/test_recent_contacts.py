import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from aiohttp import web

from huddle_phone.app import Service
from huddle_phone.state import State


class RecentContactsTests(unittest.TestCase):
    def test_history_survives_restart_without_being_reordered_by_old_observations(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp)/"huddles.db")
            state = State(path)
            state.claim_outgoing("request","UOTHER","ring",1000)
            self.assertEqual([],state.recent_contacts())
            state.record_contact("U123",1000)
            state.record_contact("U456",2000)
            state.record_contact("U123",500)
            state.close()
            state = State(path)
            self.assertEqual([{"user_id":"U456","connected_at":2000},
                              {"user_id":"U123","connected_at":1000}],state.recent_contacts())
            state.record_contact("U123",3000)
            self.assertEqual("U123",state.recent_contacts()[0]["user_id"])
            self.assertEqual(2,len(state.recent_contacts()))
            state.close()

    def test_recent_history_rejects_invalid_identifiers_or_times(self):
        state = State(":memory:")
        try:
            for user,timestamp in [("U123\nsecret",1000),("not-a-user",1000),("U123",float("nan")),("U123",-1)]:
                with self.assertRaises(ValueError):
                    state.record_contact(user,timestamp)
            self.assertEqual([],state.recent_contacts())
        finally:
            state.close()


class RecentControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_recent_control_is_authenticated_read_only(self):
        service = Service(SimpleNamespace(state_path=":memory:",operator_secret="test-secret"))
        service.slack = SimpleNamespace(search_users=AsyncMock())
        service.state.record_contact("U123",1000)
        request = SimpleNamespace(headers={"Authorization":"Bearer test-secret"},json=AsyncMock(return_value={"command":"recent"}))
        try:
            response = await service.control(request)
            self.assertEqual({"contacts":[{"user_id":"U123","connected_at":1000}]},json.loads(response.body))
            service.slack.search_users.assert_not_awaited()
            request.headers = {}
            with self.assertRaises(web.HTTPForbidden):
                await service.control(request)
        finally:
            service.state.close()

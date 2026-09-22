import tempfile
import unittest
from pathlib import Path

from huddle_phone.huddles import from_message
from huddle_phone.state import State


def message(**room):
    return {"type": "message", "subtype": "huddle_thread", "channel": "C123",
            "team": "T123", "ts": "1000.000001",
            "room": {"id": "R123", "call_family": "huddle", "date_start": 1000,
                     "date_end": 0, "has_ended": False, **room}}


def parse(value):
    return from_message(value, team_id="T123", channel_id="C123")


class HuddleTests(unittest.TestCase):
    def test_history_start_and_changed_end(self):
        start = parse(message())
        self.assertFalse(start.ended)
        self.assertEqual(start.key, "T123:C123:R123")
        end = parse({"type": "message", "subtype": "message_changed", "channel": "C123",
                     "message": message(has_ended=True, date_end=1050)})
        self.assertTrue(end.ended)

    def test_wrong_channel_or_workspace_cannot_dial(self):
        for field, value in (("channel", "C999"), ("team", "T999")):
            item = message()
            item[field] = value
            self.assertIsNone(parse(item))
            wrapped = {"type": "message", "subtype": "message_changed", "message": item}
            self.assertIsNone(parse(wrapped))

    def test_user_status_is_not_a_channel_start_event(self):
        self.assertIsNone(parse({"type": "user_huddle_changed", "user": {
            "profile": {"huddle_state": "in_a_huddle", "huddle_state_call_id": "R123"}}}))

    def test_unknown_or_malformed_room_never_dials(self):
        for room in (None, {}, {"id": "R123"}, {"call_family": "other"}):
            item = message()
            item["room"] = room
            self.assertIsNone(parse(item))
        for change in ({"date_start": "nan"}, {"date_start": "inf"}, {"id": "R1\nHeader: x"},
                       {"has_ended": None}, {"date_end": "bad"}):
            self.assertIsNone(parse(message(**change)))


class StateTests(unittest.TestCase):
    def test_retries_and_restart_do_not_ring_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.sqlite3")
            state = State(path)
            huddle = parse(message())
            self.assertTrue(state.claim(huddle, 1002, 120))
            self.assertFalse(state.claim(huddle, 1003, 120))
            state.close()
            state = State(path)
            self.assertFalse(state.claim(huddle, 1004, 120))
            state.close()

    def test_end_before_start_prevents_late_ring(self):
        state = State(":memory:")
        state.observe(parse(message(has_ended=True, date_end=1001)), 1002)
        self.assertFalse(state.claim(parse(message()), 1003, 120))
        self.assertTrue(state.is_ended(parse(message())))
        state.close()

    def test_stale_and_future_huddles_never_ring(self):
        for now in (500, 1121):
            state = State(":memory:")
            self.assertFalse(state.claim(parse(message()), now, 120))
            state.close()


if __name__ == "__main__":
    unittest.main()

import unittest
import uuid
from unittest.mock import AsyncMock

from pbx import PBX


class DisplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_updates_exact_audio_call_without_touching_another_channel(self):
        call_id = str(uuid.uuid4())
        pbx = PBX()
        pbx.command = AsyncMock(side_effect=[
            f"SCCP/6738-00000001!x!s!1!Up!AudioSocket!{uuid.uuid4()},127.0.0.1:9095!0\n"
            f"SCCP/6738-00000002!x!s!1!Up!AudioSocket!{call_id},127.0.0.1:9095!0\n",
            "", "", "", "",
        ])
        self.assertTrue(await pbx.connected_line(call_id, 'Alex "AJ"\r\nFriend', "880012"))
        updates = [call.args[-1] for call in pbx.command.await_args_list[1:]]
        self.assertTrue(all("SCCP/6738-00000002" in update for update in updates))
        self.assertTrue(all("SCCP/6738-00000001" not in update for update in updates))
        self.assertIn('CONNECTEDLINE(name,i) "Alex AJ Friend"', updates[-2])
        self.assertIn('CONNECTEDLINE(num) "880012"', updates[-1])

    async def test_stale_audio_call_cannot_change_current_phone_display(self):
        pbx = PBX()
        pbx.command = AsyncMock(return_value=f"SCCP/6738-00000001!x!s!1!Up!AudioSocket!{uuid.uuid4()},127.0.0.1:9095!0\n")
        self.assertFalse(await pbx.connected_line(str(uuid.uuid4()), "Old call", "880012"))
        pbx.command.assert_awaited_once()

    async def test_invalid_destination_cannot_inject_a_pbx_command(self):
        pbx = PBX()
        pbx.command = AsyncMock()
        with self.assertRaises(ValueError):
            await pbx.connected_line(str(uuid.uuid4()), "Name", "0\ncore stop now")
        pbx.command.assert_not_awaited()

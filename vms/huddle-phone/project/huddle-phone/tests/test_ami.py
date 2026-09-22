import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from huddle_phone.ami import AMI, encode, phone_name


class AmiTests(unittest.IsolatedAsyncioTestCase):
    async def test_correlates_async_origination_events_and_hangs_up_only_its_channel(self):
        received = []
        async def server(reader, writer):
            writer.write(b"Asterisk Call Manager/5.0\r\n")
            await writer.drain()
            try:
                while True:
                    raw = await reader.readuntil(b"\r\n\r\n")
                    fields = dict(line.split(": ", 1) for line in raw.decode().strip().split("\r\n"))
                    received.append(fields)
                    action_id = fields["ActionID"]
                    writer.write(encode([("Response", "Success"), ("ActionID", action_id)]))
                    await writer.drain()
                    # Cancellation can occur AFTER the Originate acknowledgement
                    # but BEFORE the channel is announced.
                    if fields["Action"] == "Originate":
                        await asyncio.sleep(0.02)
                        writer.write(encode([("Event", "Newchannel"), ("Uniqueid", "call-123"),
                                             ("Channel", "SCCP/6738-00000007")]))
                    if fields["Action"] == "Originate":
                        writer.write(encode([("Event", "OriginateResponse"), ("ActionID", action_id),
                                             ("Response", "Success"), ("Channel", "SCCP/6738-00000007")]))
                    await writer.drain()
            except asyncio.IncompleteReadError:
                pass
            finally:
                writer.close()
                await writer.wait_closed()
        listener = await asyncio.start_server(server, "127.0.0.1", 0)
        config = SimpleNamespace(ami_host="127.0.0.1", ami_port=listener.sockets[0].getsockname()[1],
                                 ami_user="test", ami_secret="test-secret", phone_channel="SCCP/6738",
                                 ring_seconds=30, audio_port=9093, max_call_seconds=600)
        ami = AMI(config)
        try:
            await ami.connect()
            await ami.originate("call-123")
            await ami.hangup("call-123")
            self.assertEqual(received[-1]["Action"], "Hangup")
            self.assertEqual(received[-1]["Channel"], "SCCP/6738-00000007")
            self.assertEqual(received[1]["ChannelId"], "call-123")
        finally:
            await ami.close()
            listener.close()
            await listener.wait_closed()

    def test_newlines_cannot_inject_an_ami_action(self):
        with self.assertRaises(ValueError):
            encode([("CallerID", "Slack\r\nAction: Command")])

    async def test_slack_name_sets_ringing_and_connected_party_information(self):
        ami = AMI(SimpleNamespace(phone_channel="SCCP/6738", ring_seconds=30,
                                  audio_port=9094, max_call_seconds=600))
        ami.action = AsyncMock()
        await ami.originate("call-123", caller_name='Alex "AJ" <555>\r\nSmith')
        fields = ami.action.await_args.args[0]
        self.assertIn(("CallerID", '"Alex AJ 555 Smith" <612>'), fields)
        self.assertIn(("Variable", 'CONNECTEDLINE(all)="Alex AJ 555 Smith" <612>'), fields)
        self.assertIn(("Variable", "CONNECTEDLINE(name-pres,i)=allowed"), fields)
        self.assertIn(("Variable", "CONNECTEDLINE(num-pres,i)=allowed"), fields)
        encode(fields)  # No header injection even with a multiline Slack name.

    def test_phone_name_fits_sccp_without_splitting_a_unicode_character(self):
        self.assertEqual("Slack huddle", phone_name('\x00\r\n<>"'))
        self.assertEqual("é" * 19, phone_name("é" * 100))

"""Runs inside the bot image sharing an isolated test Asterisk network namespace.
The phone endpoint is a Local channel; this cannot reach a physical handset.
"""

import asyncio
from types import SimpleNamespace
import uuid

import aiohttp

from huddle_phone.ami import AMI
from huddle_phone.media import MediaCall, MediaServer


async def main():
    config = SimpleNamespace(ami_host="127.0.0.1", ami_port=5038, ami_user="smoke", ami_secret="smoke-local-only",
                             phone_channel="Local/s@smoke-phone/n", ring_seconds=5, max_call_seconds=30,
                             http_port=8099, audio_port=9093)
    server = MediaServer(config, lambda: {"healthy": True})
    call = MediaCall(str(uuid.uuid4()))
    server.active = call
    ami = AMI(config)
    async with aiohttp.ClientSession() as http:
        try:
            await server.start()
            await ami.connect()
            ws = await http.ws_connect(f"http://127.0.0.1:8099/media/{call.call_id}",
                                       protocols=["huddle-audio", call.token], origin="http://127.0.0.1:8099")
            await ami.originate(call.call_id)
            await asyncio.wait_for(call.answered.wait(), 5)
            assert call.call_id in ami.channels, "Asterisk did not honor ChannelId"
            assert ami.channels[call.call_id].startswith("Local/s@smoke-phone")
            await ws.send_bytes(b"\x01\x01" * 160)
            await asyncio.sleep(0.1)
            assert call.tx_bytes > 0
            await ami.hangup(call.call_id)
            await asyncio.wait_for(call.done.wait(), 5)
            print("PASS: real Asterisk AMI Originate, custom ChannelId, huddle dialplan, AudioSocket answer and targeted hangup")
        finally:
            await ami.close()
            await server.close()


asyncio.run(main())

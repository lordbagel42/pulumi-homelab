"""Offline Chromium / bundled Chime SDK / Web Audio smoke test.

Run inside the built container with --network none and tests mounted at /tests.
No Slack credentials or actual huddle is used.
"""

import asyncio
import socket
from types import SimpleNamespace
import uuid

from huddle_phone.browser import ChimeRuntime
from huddle_phone.media import MediaCall, MediaServer


def port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


async def main():
    config = SimpleNamespace(http_port=port(), audio_port=port(), chromium="/usr/bin/chromium")
    server = MediaServer(config, lambda: {"healthy": True})
    runtime = ChimeRuntime(config)
    call = MediaCall(str(uuid.uuid4()))
    server.active = call
    try:
        await server.start()
        await runtime.prepare(call.call_id, call.token)
        assert call.socket is not None
        assert await runtime.healthy()
        # Run the REAL worklet in Chromium and capture a 1 kHz test tone as PCM.
        result = await runtime.page.evaluate("""async () => {
          const audio = new AudioContext({sampleRate: 8000});
          await audio.audioWorklet.addModule('/pcm-worklet.js');
          const output = new AudioWorkletNode(audio, 'phone-output');
          const oscillator = audio.createOscillator();
          oscillator.frequency.value = 1000;
          const gain = audio.createGain(); gain.gain.value = 0.25;
          oscillator.connect(gain).connect(output).connect(audio.destination);
          const capture = new Promise((resolve, reject) => {
            const timeout = setTimeout(() => reject(Error('Audio worklet did not produce PCM')), 5000);
            output.port.onmessage = ({data}) => {
              const view = new DataView(data);
              let peak = 0;
              for (let i=0; i<data.byteLength; i+=2) peak = Math.max(peak, Math.abs(view.getInt16(i, true)));
              if (peak > 1000) { clearTimeout(timeout); resolve({bytes:data.byteLength, peak}); }
            };
          });
          oscillator.start(); await audio.resume();
          const result = await capture;
          oscillator.stop(); await audio.close();
          return result;
        }""")
        assert result["bytes"] == 320 and result["peak"] > 1000, result
        # Exercise the real SDK constructor and both audio bindings. Importing
        # the bundle alone missed CSPMonitor's missing browser `global` alias.
        # Hold signaling locally; synthetic credentials never leave this test.
        await runtime.page.route_web_socket("wss://127.0.0.1:1/**", lambda _: None)
        await runtime.page.evaluate("""credentials => {
          window.smokeJoinResult = null;
          window.joinHuddle(credentials).then(result => { window.smokeJoinResult = result; });
        }""", {
            "meeting": {"MeetingId": str(uuid.uuid4()), "MediaPlacement": {
                "AudioHostUrl": "127.0.0.1", "SignalingUrl": "wss://127.0.0.1:1/",
                "TurnControlUrl": "https://127.0.0.1:1/"}},
            "attendee": {"AttendeeId": str(uuid.uuid4()), "JoinToken": "synthetic-test-token"},
        })
        await runtime.page.wait_for_function(
            "window.chimeStatus.stage === 'connecting' || window.smokeJoinResult !== null", timeout=5000)
        status = await runtime.page.evaluate("window.chimeStatus")
        assert status == {"stage": "connecting", "errorType": None, "statusCode": None}, status
        assert await runtime.healthy()
        print("PASS: real Chime session initializes through audio input/output to signaling; authenticated audio socket and AudioWorklet PCM work")
    finally:
        await runtime.close()
        await server.close()


asyncio.run(main())

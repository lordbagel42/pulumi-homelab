"""A provider outage must not permanently silence a connected handset."""
import asyncio
import io
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import wave

from audio import Speech, SpeechServiceError
from phone_bridge import Call


def wav_bytes():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\x10\x00" * 160)
    return stream.getvalue()


class SpeechFallbackTests(unittest.TestCase):
    def test_provider_failure_uses_local_audio_without_losing_route(self):
        speech = Speech.__new__(Speech)
        speech.platform_url, speech.route = "http://switchboard.invalid", "codex"
        speech.platform_request = Mock(side_effect=[SpeechServiceError(503), wav_bytes()])
        self.assertEqual(b"\x10\x00" * 160, speech.synthesize("A reply", route="slack-operator"))
        self.assertEqual({"text": "A reply", "route": "slack-operator", "provider": "piper"},
                         speech.platform_request.call_args.args[1])

    def test_auth_failure_does_not_retry(self):
        speech = Speech.__new__(Speech)
        speech.platform_url, speech.route = "http://switchboard.invalid", "codex"
        speech.platform_request = Mock(side_effect=SpeechServiceError(401))
        with self.assertRaises(SpeechServiceError):
            speech.synthesize("A reply")
        speech.platform_request.assert_called_once()


class PlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def test_later_utterance_plays_after_one_failed_synthesis(self):
        class Writer:
            def __init__(self):
                self.frames = []
            def write(self, value):
                self.frames.append(value)
            async def drain(self):
                pass
        class Bridge:
            sessions = SimpleNamespace()
            attempts = 0
            async def synthesize(self, text):
                self.attempts += 1
                if self.attempts == 1:
                    raise RuntimeError("Provider temporarily unavailable")
                return b"\x10\x00" * 160
        writer = Writer()
        call = Call(Bridge(), None, writer)
        events = []
        call.status = lambda event, **kwargs: events.append(event)
        call.say("Failed greeting")
        call.say("Recovered reply")
        player = asyncio.create_task(call.player())
        try:
            await asyncio.wait_for(call.output.join(), 2)
            self.assertEqual(320, call.tx_bytes)
            self.assertEqual(1, len(writer.frames))
            self.assertIn("speech_failed", events)
            self.assertFalse(player.done())
        finally:
            call.closed = True
            player.cancel()
            await asyncio.gather(player, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()

import audioop
import unittest

import numpy as np
from piper import PiperVoice

from audio import ROOT, Speech
from speech_gate import SpeechEndpoint, SpeechGate


class SpeechGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.speech = Speech.__new__(Speech)
        cls.speech.piper = PiperVoice.load(str(ROOT / "models/en_US-lessac-medium.onnx"))

    def feed(self, pcm):
        endpoint = SpeechEndpoint()
        interruptions, utterances = 0, []
        for offset in range(0, len(pcm), 320):
            result = endpoint.feed(pcm[offset:offset + 320].ljust(320, b"\0"))
            interruptions += endpoint.barge_ready
            if result:
                utterances.append(result)
        return interruptions, utterances

    def test_loud_breath_like_noise_does_not_interrupt(self):
        rng = np.random.default_rng(20260919)
        samples = rng.normal(size=8000 * 6)
        # Broad filtered air noise with repeated inhale/exhale envelopes.
        samples = np.convolve(samples, np.ones(7) / 7, mode="same")
        envelope = np.maximum(0, np.sin(np.arange(len(samples)) / 8000 * np.pi)) ** 2
        samples = samples / np.std(samples) * 2800 * envelope
        pcm = np.clip(samples, -32768, 32767).astype("<i2").tobytes() + bytes(24000)
        interruptions, utterances = self.feed(pcm)
        self.assertEqual(0, interruptions)
        self.assertEqual([], utterances)

    def test_real_synthesized_words_interrupt_and_keep_onset(self):
        pcm = self.speech.synthesize("Stop talking. I need to change the task.")
        interruptions, utterances = self.feed(pcm + bytes(24000))
        self.assertGreater(interruptions, 0)
        self.assertEqual(1, len(utterances))
        self.assertGreater(len(utterances[0]), len(pcm) * 0.8)

    def test_short_interruption_survives_gaps_between_syllables(self):
        for words in ("Stop talking.", "Wait a second."):
            with self.subTest(words=words):
                gate = SpeechGate()
                pcm = self.speech.synthesize(words)
                first = None
                for offset in range(0, len(pcm), 320):
                    gate.feed(pcm[offset:offset + 320].ljust(320, b"\0"))
                    if gate.barge_ready:
                        first = offset / 16000
                        break
                self.assertIsNotNone(first, "Short speech must interrupt playback")
                self.assertLess(first, 0.7)

    def test_quiet_g711_speech_interrupts(self):
        # Model the phone's codec and a quiet caller, not just loud clean TTS.
        pcm = audioop.mul(self.speech.synthesize("Stop talking."), 2, 0.05)
        pcm = audioop.ulaw2lin(audioop.lin2ulaw(pcm, 2), 2)
        gate = SpeechGate()
        for offset in range(0, len(pcm), 320):
            gate.feed(pcm[offset:offset + 320].ljust(320, b"\0"))
            if gate.barge_ready:
                self.assertLess(offset / 16000, 0.7)
                return
        self.fail("Quiet G.711 speech did not interrupt")


if __name__ == "__main__":
    unittest.main()

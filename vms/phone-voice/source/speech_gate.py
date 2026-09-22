"""Streaming Silero speech detection: reject breaths before interrupting audio."""

import audioop
from collections import deque

import numpy as np
from faster_whisper.vad import get_vad_model


class SpeechGate:
    """Use the pinned faster-whisper Silero v6 model with per-call RNN state."""

    def __init__(self):
        self.session = get_vad_model().session
        self.h = np.zeros((1, 1, 128), dtype=np.float32)
        self.c = np.zeros((1, 1, 128), dtype=np.float32)
        self.context = np.zeros(64, dtype=np.float32)
        self.buffer = bytearray()
        self.rate_state = None
        self.probability = 0.0
        self.recent_speech = deque(maxlen=10)

    def feed(self, frame):
        pcm, self.rate_state = audioop.ratecv(frame, 2, 1, 8000, 16000, self.rate_state)
        self.buffer.extend(pcm)
        while len(self.buffer) >= 1024:
            chunk = np.frombuffer(bytes(self.buffer[:1024]), dtype="<i2").astype(np.float32) / 32768
            del self.buffer[:1024]
            data = np.concatenate((self.context, chunk)).reshape(1, 576)
            output, self.h, self.c = self.session.run(None, {"input": data, "h": self.h, "c": self.c})
            self.context = chunk[-64:]
            self.probability = float(np.asarray(output).reshape(-1)[-1])
        rms = audioop.rms(frame, 2)
        # Require neural speech confidence and audible energy. Permit the short
        # gaps between syllables: resetting at every gap misses "stop talking".
        strong = self.probability >= 0.65 and rms >= 90
        self.recent_speech.append(strong)
        return self.probability >= 0.55 and rms >= 90

    @property
    def barge_ready(self):
        return bool(self.recent_speech and self.recent_speech[-1]
                    and sum(self.recent_speech) >= 6)  # 120 ms within 200 ms.


class SpeechEndpoint:
    def __init__(self):
        self.gate = SpeechGate()
        self.reset()

    def reset(self):
        self.pre = deque(maxlen=25)  # Keep onset while the neural gate warms up.
        self.frames = []
        self.started = False
        self.run = self.voiced = self.silent = 0

    @property
    def barge_ready(self):
        return self.gate.barge_ready

    def feed(self, frame):
        speech = self.gate.feed(frame)
        if not self.started:
            self.pre.append(frame)
            self.run = self.run + 1 if speech else 0
            if self.run >= 3:
                self.started = True
                self.frames = list(self.pre)
                self.voiced = self.run
            return None
        self.frames.append(frame)
        if speech:
            self.voiced += 1
            self.silent = 0
        else:
            self.silent += 1
        # Allow a natural pause without replying over the caller mid-sentence.
        if self.silent >= 60 or len(self.frames) >= 1500:
            return self.finish()
        return None

    def finish(self):
        result = b"".join(self.frames) if self.voiced >= 8 else None
        self.reset()
        return result

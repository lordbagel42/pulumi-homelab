import {
  ConsoleLogger, DefaultDeviceController, DefaultMeetingSession,
  LogLevel, MeetingSessionConfiguration,
} from "amazon-chime-sdk-js";

const logger = new ConsoleLogger("huddle-phone", LogLevel.OFF);
let context, socket, input, output, phoneMicrophone, meetingSession, source;
let stopping = false;
window.mediaReady = false;
window.mediaFailed = false;
window.chimeStatus = { stage: "idle", errorType: null, statusCode: null };
const fail = () => { if (!stopping) window.mediaFailed = true; };

window.preparePhone = async ({ callId, token }) => {
  socket = new WebSocket(`ws://${location.host}/media/${callId}`, ["huddle-audio", token]);
  socket.binaryType = "arraybuffer";
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("Media socket timeout")), 5000);
    socket.onopen = () => { clearTimeout(timer); resolve(); };
    socket.onerror = () => { clearTimeout(timer); reject(new Error("Media socket failed")); };
  });
  socket.onclose = fail;
  socket.onerror = fail;
  context = new AudioContext({ sampleRate: 8000 });
  if (context.sampleRate !== 8000) throw new Error("Browser does not support 8 kHz audio");
  await context.audioWorklet.addModule("/pcm-worklet.js");
  input = new AudioWorkletNode(context, "phone-input", {
    numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1],
  });
  phoneMicrophone = context.createMediaStreamDestination();
  input.connect(phoneMicrophone);
  socket.onmessage = ({ data }) => {
    if (data instanceof ArrayBuffer) input.port.postMessage(data, [data]);
  };
  output = new AudioWorkletNode(context, "phone-output", { channelCount: 1, channelCountMode: "explicit" });
  const silence = context.createGain();
  silence.gain.value = 0;
  output.connect(silence).connect(context.destination);
  output.port.onmessage = ({ data }) => {
    if (socket.readyState === WebSocket.OPEN && socket.bufferedAmount < 16000) socket.send(data);
  };
  await context.resume();
  window.mediaReady = true;
};

window.joinHuddle = async ({ meeting, attendee }) => {
  window.chimeStatus = { stage: "configure", errorType: null, statusCode: null };
  try {
    // These are Slack's native huddle credentials, from call.free_willy.
    // No AWS credentials, CreateMeeting, PSTN service or second meeting.
    meetingSession = new DefaultMeetingSession(
      new MeetingSessionConfiguration(meeting, attendee), logger, new DefaultDeviceController(logger),
    );
    meetingSession.audioVideo.addAudioMixObserver({
      meetingAudioStreamBecameActive: stream => {
        source?.disconnect();
        source = context.createMediaStreamSource(stream);
        source.connect(output);
      },
      meetingAudioStreamBecameInactive: () => { source?.disconnect(); },
    });
    // Bind a muted element so the SDK publishes its audio-mix stream to the
    // observer. The stream is captured by Web Audio, not played to a device.
    window.chimeStatus.stage = "audio-output";
    await meetingSession.audioVideo.bindAudioElement(document.getElementById("phone-capture"));
    window.chimeStatus.stage = "audio-input";
    await meetingSession.audioVideo.startAudioInput(phoneMicrophone.stream);
    window.chimeStatus.stage = "connecting";
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("Chime connection timeout")), 25000);
      meetingSession.audioVideo.addObserver({
        audioVideoDidStart: () => { clearTimeout(timer); resolve(); },
        audioVideoDidStop: status => {
          const code = status?.statusCode();
          window.chimeStatus.statusCode = Number.isInteger(code) ? code : null;
          clearTimeout(timer); fail(); reject(new Error("Chime disconnected"));
        },
      });
      meetingSession.audioVideo.start();
    });
    meetingSession.audioVideo.realtimeUnmuteLocalAudio();
    window.chimeStatus.stage = "connected";
    return { ok: true };
  } catch (error) {
    fail();
    // SDK messages and stacks may contain join tokens or signed URLs. Return
    // only fixed stage/type labels and a numeric status for server diagnostics.
    const types = ["Error", "TypeError", "ReferenceError", "NotFoundError", "NotReadableError",
      "PermissionDeniedError", "NotAllowedError", "NotSupportedError", "OverconstrainedError",
      "GetUserMediaError", "TimeoutError", "AbortError"];
    window.chimeStatus.errorType = types.includes(error?.name) ? error.name : "Error";
    return { ok: false, ...window.chimeStatus };
  }
};

window.stopHuddle = async () => {
  stopping = true;
  window.mediaReady = false;
  if (meetingSession) {
    await new Promise(resolve => {
      const timer = setTimeout(resolve, 3000);
      meetingSession.audioVideo.addObserver({ audioVideoDidStop: () => { clearTimeout(timer); resolve(); } });
      meetingSession.audioVideo.stop(); // Sends the Chime leave frame.
    });
    await meetingSession.deviceController.destroy();
  }
  source?.disconnect();
  socket?.close();
  if (context && context.state !== "closed") await context.close();
};

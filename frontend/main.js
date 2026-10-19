// Browser test client: talk to the agent without a phone.
// Mic -> 16 kHz PCM (pcm-processor.js) -> /web/ws -> 24 kHz PCM played back.
// Protocol: see adk_agent/browser_stream.py.

const AGENT_OUTPUT_RATE = 24000;

const $ = (id) => document.getElementById(id);
const callButton = $("call");
const statusEl = $("status");
const transcriptEl = $("transcript");

let ws = null;
let audioCtx = null;
let micStream = null;
let nextStartTime = 0;
let hangupRequested = false;
const playing = new Set();
const openLines = { user: null, agent: null };

function setStatus(text, state = "idle") {
  statusEl.textContent = text;
  document.body.dataset.state = state;
}

// ---------- Transcript ----------

function showTranscript({ role, text, finished }) {
  if (text && !openLines[role]) {
    const line = document.createElement("p");
    line.className = "line " + role;
    transcriptEl.appendChild(line);
    openLines[role] = line;
  }
  if (openLines[role]) openLines[role].textContent += text;
  if (finished) openLines[role] = null;
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

// ---------- Agent voice ----------

function playPcm(arrayBuffer) {
  const int16 = new Int16Array(arrayBuffer);
  const buffer = audioCtx.createBuffer(1, int16.length, AGENT_OUTPUT_RATE);
  const channel = buffer.getChannelData(0);
  for (let i = 0; i < int16.length; i++) channel[i] = int16[i] / 0x8000;

  const source = audioCtx.createBufferSource();
  source.buffer = buffer;
  source.connect(audioCtx.destination);
  // Queue chunks back to back so they play without gaps.
  nextStartTime = Math.max(nextStartTime, audioCtx.currentTime);
  source.start(nextStartTime);
  nextStartTime += buffer.duration;

  playing.add(source);
  setStatus("Agent speaking", "speaking");
  source.onended = () => {
    playing.delete(source);
    if (playing.size) return;
    if (hangupRequested) endCall("Agent hung up");
    else if (ws) setStatus("Listening", "listening");
  };
}

function stopPlayback() {
  for (const source of playing) source.stop();
  playing.clear();
  nextStartTime = 0;
}

// ---------- Call ----------

function handleMessage(message) {
  switch (message.type) {
    case "transcript":
      showTranscript(message);
      break;
    case "interrupted": // caller barged in
      stopPlayback();
      setStatus("Listening", "listening");
      break;
    case "hangup": // hang up once the goodbye has played
      hangupRequested = true;
      if (!playing.size) endCall("Agent hung up");
      break;
  }
}

async function startMic() {
  await audioCtx.audioWorklet.addModule("pcm-processor.js");
  micStream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  });
  const worklet = new AudioWorkletNode(audioCtx, "pcm-processor");
  worklet.port.onmessage = (event) => {
    if (ws?.readyState === WebSocket.OPEN) ws.send(event.data);
  };
  audioCtx.createMediaStreamSource(micStream).connect(worklet);
  worklet.connect(audioCtx.destination); // outputs silence; keeps the node running
}

async function startCall() {
  callButton.disabled = true;
  setStatus("Connecting…", "connecting");
  try {
    audioCtx = new AudioContext();
    await startMic();
  } catch (error) {
    endCall("Microphone unavailable: " + error.message);
    return;
  }

  const socket = new WebSocket(new URL("ws", location.href.replace(/^http/, "ws")));
  socket.binaryType = "arraybuffer";
  socket.onopen = () => {
    setStatus("Listening", "listening");
    callButton.textContent = "Hang up";
    callButton.disabled = false;
  };
  socket.onmessage = (event) =>
    typeof event.data === "string" ? handleMessage(JSON.parse(event.data)) : playPcm(event.data);
  socket.onclose = () => {
    if (ws === socket) endCall("Call ended");
  };
  ws = socket;
}

function endCall(status) {
  const socket = ws;
  ws = null;
  socket?.close();
  micStream?.getTracks().forEach((track) => track.stop());
  stopPlayback();
  audioCtx?.close();
  audioCtx = micStream = null;
  hangupRequested = false;
  openLines.user = openLines.agent = null;

  setStatus(status);
  callButton.textContent = "Start call";
  callButton.disabled = false;
}

callButton.addEventListener("click", () => (ws ? endCall("Call ended") : startCall()));

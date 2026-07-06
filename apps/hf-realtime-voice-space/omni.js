// @ts-check

const $ = (id) => /** @type {HTMLElement} */ (document.getElementById(id));

const stateEl = $("state");
const modelEl = $("model");
const endpointEl = $("endpoint");
const connectBtn = /** @type {HTMLButtonElement} */ ($("connect"));
const talkBtn = /** @type {HTMLButtonElement} */ ($("talk"));
const endTurnBtn = /** @type {HTMLButtonElement} */ ($("end-turn"));
const disconnectBtn = /** @type {HTMLButtonElement} */ ($("disconnect"));
const clearLogBtn = /** @type {HTMLButtonElement} */ ($("clear-log"));
const logEl = $("log");
const inputText = $("input-text");
const outputText = $("output-text");
const meterFill = $("meter-fill");
const orb = $("orb");

/** @type {WebSocket | null} */
let ws = null;
/** @type {AudioContext | null} */
let audioContext = null;
/** @type {MediaStream | null} */
let micStream = null;
/** @type {MediaStreamAudioSourceNode | null} */
let micSource = null;
/** @type {ScriptProcessorNode | null} */
let micProcessor = null;
let capturing = false;
let playCursor = 0;
let model = "";
let reconnectGuard = false;

function setState(text, kind = "") {
  stateEl.textContent = text;
  stateEl.className = kind;
}

function log(message, kind = "") {
  const ts = new Date().toLocaleTimeString();
  const line = `[${ts}] ${message}`;
  logEl.textContent = `${logEl.textContent ? `${logEl.textContent}\n` : ""}${line}`;
  logEl.scrollTop = logEl.scrollHeight;
  if (kind === "error") stateEl.className = "error";
}

function setConnectedUi(connected) {
  connectBtn.disabled = connected;
  talkBtn.disabled = !connected;
  endTurnBtn.disabled = !connected;
  disconnectBtn.disabled = !connected;
}

function ensureAudioContext() {
  if (!audioContext) {
    audioContext = new AudioContext();
    playCursor = audioContext.currentTime;
  }
  return audioContext.resume().then(() => audioContext);
}

function base64FromBytes(bytes) {
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, /** @type {number[]} */ (
      /** @type {unknown} */ (bytes.subarray(i, i + chunk))
    ));
  }
  return btoa(binary);
}

function bytesFromBase64(b64) {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

function downsampleTo16k(input, inputRate) {
  const outputRate = 16000;
  if (inputRate === outputRate) return input;
  const ratio = inputRate / outputRate;
  const outputLength = Math.max(1, Math.floor(input.length / ratio));
  const output = new Float32Array(outputLength);
  for (let i = 0; i < outputLength; i += 1) {
    const start = Math.floor(i * ratio);
    const end = Math.min(input.length, Math.floor((i + 1) * ratio));
    let sum = 0;
    let count = 0;
    for (let j = start; j < end; j += 1) {
      sum += input[j];
      count += 1;
    }
    output[i] = count ? sum / count : input[start] || 0;
  }
  return output;
}

function pcm16Base64(samples) {
  const bytes = new Uint8Array(samples.length * 2);
  const view = new DataView(bytes.buffer);
  for (let i = 0; i < samples.length; i += 1) {
    const clamped = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(i * 2, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true);
  }
  return base64FromBytes(bytes);
}

function meter(samples) {
  let peak = 0;
  for (let i = 0; i < samples.length; i += 1) peak = Math.max(peak, Math.abs(samples[i]));
  meterFill.style.width = `${Math.min(100, Math.round(peak * 160))}%`;
}

function sendJson(payload) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return false;
  ws.send(JSON.stringify(payload));
  return true;
}

function sendSessionUpdate() {
  sendJson({ type: "session.update", model });
  sendJson({ type: "input_audio_buffer.commit", final: false });
}

async function diagnostics() {
  const resp = await fetch("/api/qwen-omni/diagnostics", { cache: "no-store" });
  if (!resp.ok) throw new Error(`Diagnostics failed: HTTP ${resp.status}`);
  return await resp.json();
}

function renderDiagnostics(diag, options = {}) {
  const shouldLog = options.log !== false;
  model = String(diag.effectiveModel || diag.model || "");
  modelEl.textContent = model || "Not configured";
  endpointEl.textContent = String(diag.realtimeUrl || diag.baseUrl || "Local only");
  if (!shouldLog) return;
  log(`${diag.status}: ${diag.message}`);
  if (Array.isArray(diag.models) && diag.models.length) {
    const found = diag.modelFound ? "found" : diag.suggestedModel ? `using ${diag.suggestedModel}` : "not listed";
    log(`models: ${diag.models.slice(0, 6).join(", ")}${diag.models.length > 6 ? ", ..." : ""} (${found})`);
  }
}

async function connect() {
  if (reconnectGuard || (ws && ws.readyState === WebSocket.OPEN)) return;
  reconnectGuard = true;
  setState("Checking");
  setConnectedUi(false);
  try {
    await ensureAudioContext();
    const diag = await diagnostics();
    renderDiagnostics(diag);
    if (diag.status !== "realtime_supported") {
      const hint = diag.status === "auth_invalid"
        ? "LM Studio token rejected."
        : diag.status === "realtime_unsupported" && String(diag.message || "").includes("LM Studio")
          ? "LM Studio realtime unsupported."
        : String(diag.message || "Qwen3 Omni realtime endpoint is unavailable.");
      setState(hint, "error");
      return;
    }

    const protocol = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${protocol}://${location.host}/api/qwen-omni/realtime`);
    ws.onopen = () => {
      setState("Connected");
      setConnectedUi(true);
      log("proxy socket opened");
    };
    ws.onmessage = (event) => handleMessage(event.data);
    ws.onerror = () => {
      setState("WebSocket error", "error");
      log("WebSocket error", "error");
    };
    ws.onclose = () => {
      stopCapture(false);
      setConnectedUi(false);
      setState("Disconnected");
      log("socket closed");
      ws = null;
    };
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    setState(message, "error");
    log(message, "error");
  } finally {
    reconnectGuard = false;
  }
}

function handleMessage(raw) {
  let event;
  try {
    event = JSON.parse(String(raw));
  } catch {
    log("non-JSON upstream message");
    return;
  }

  switch (event.type) {
    case "proxy.ready":
      log("upstream websocket ready");
      sendSessionUpdate();
      break;
    case "session.created":
      log("session created");
      break;
    case "transcription.delta":
      inputText.textContent += event.delta || "";
      break;
    case "transcription.done":
      inputText.textContent = event.text || inputText.textContent;
      log("transcription done");
      break;
    case "response.audio.delta":
      playPcm16(event.audio || event.delta || "", Number(event.sample_rate_hz) || 24000);
      break;
    case "response.output_audio.delta":
      playPcm16(event.delta || event.audio || "", Number(event.sample_rate_hz) || 24000);
      break;
    case "response.audio.done":
    case "response.output_audio.done":
      log("audio response done");
      break;
    case "response.output_audio_transcript.delta":
    case "response.text.delta":
      outputText.textContent += event.delta || "";
      break;
    case "response.output_audio_transcript.done":
    case "response.text.done":
      outputText.textContent = event.transcript || event.text || outputText.textContent;
      break;
    case "error":
      setState(event.error || event.message || "Realtime error", "error");
      log(event.error || event.message || JSON.stringify(event), "error");
      break;
    default:
      if (event.type) log(event.type);
  }
}

function playPcm16(b64, sampleRate) {
  if (!b64 || !audioContext) return;
  const bytes = bytesFromBase64(b64);
  if (bytes.length < 2) return;
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const frameCount = Math.floor(bytes.byteLength / 2);
  const buffer = audioContext.createBuffer(1, frameCount, sampleRate);
  const channel = buffer.getChannelData(0);
  for (let i = 0; i < frameCount; i += 1) {
    const sample = view.getInt16(i * 2, true);
    channel[i] = sample < 0 ? sample / 0x8000 : sample / 0x7fff;
  }
  const source = audioContext.createBufferSource();
  source.buffer = buffer;
  source.connect(audioContext.destination);
  const now = audioContext.currentTime + 0.02;
  playCursor = Math.max(playCursor, now);
  source.start(playCursor);
  playCursor += buffer.duration;
}

async function startCapture() {
  if (!ws || ws.readyState !== WebSocket.OPEN) await connect();
  if (!ws || ws.readyState !== WebSocket.OPEN || capturing) return;
  const ctx = await ensureAudioContext();
  micStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
  });
  micSource = ctx.createMediaStreamSource(micStream);
  micProcessor = ctx.createScriptProcessor(4096, 1, 1);
  micProcessor.onaudioprocess = (event) => {
    if (!capturing || !ws || ws.readyState !== WebSocket.OPEN) return;
    const input = event.inputBuffer.getChannelData(0);
    meter(input);
    const samples16k = downsampleTo16k(input, ctx.sampleRate);
    sendJson({ type: "input_audio_buffer.append", audio: pcm16Base64(samples16k) });
  };
  micSource.connect(micProcessor);
  micProcessor.connect(ctx.destination);
  capturing = true;
  orb.classList.add("live");
  talkBtn.textContent = "Stop talking";
  setState("Listening");
  log("microphone streaming");
}

function stopCapture(sendFinal = true) {
  if (!capturing && !micStream) return;
  capturing = false;
  if (micProcessor) {
    micProcessor.disconnect();
    micProcessor.onaudioprocess = null;
    micProcessor = null;
  }
  if (micSource) {
    micSource.disconnect();
    micSource = null;
  }
  if (micStream) {
    for (const track of micStream.getTracks()) track.stop();
    micStream = null;
  }
  meterFill.style.width = "0%";
  orb.classList.remove("live");
  talkBtn.textContent = "Start talking";
  if (sendFinal) endTurn();
}

function endTurn() {
  if (!sendJson({ type: "input_audio_buffer.commit", final: true })) return;
  setState("Waiting");
  log("final commit sent");
}

function disconnect() {
  stopCapture(false);
  if (ws) ws.close();
  ws = null;
  setConnectedUi(false);
}

connectBtn.addEventListener("click", connect);
talkBtn.addEventListener("click", () => {
  if (capturing) stopCapture(true);
  else void startCapture();
});
endTurnBtn.addEventListener("click", endTurn);
disconnectBtn.addEventListener("click", disconnect);
clearLogBtn.addEventListener("click", () => {
  logEl.textContent = "";
  inputText.textContent = "";
  outputText.textContent = "";
});

void diagnostics()
  .then((diag) => renderDiagnostics(diag, { log: false }))
  .catch((err) => log(err instanceof Error ? err.message : String(err), "error"));

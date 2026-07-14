import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { WakeWordController } from "../apps/hf-realtime-voice-space/ui/wake-word.js";
import { S2sWsRealtimeClient } from "../apps/hf-realtime-voice-space/ws/s2s-ws-client.js";

class FakeSocket extends EventTarget {
  constructor() {
    super();
    this.readyState = 1;
    this.binaryType = "";
    this.sent = [];
  }

  send(value) { this.sent.push(value); }

  close() {
    this.readyState = 3;
    this.dispatchEvent(new Event("close"));
  }

  message(value) {
    this.dispatchEvent(new MessageEvent("message", { data: JSON.stringify(value) }));
  }
}

function fakeScheduler() {
  let nextId = 1;
  const timers = new Map();
  return {
    setTimeoutFn(callback, delay) {
      const id = nextId++;
      timers.set(id, { callback, delay });
      return id;
    },
    clearTimeoutFn(id) { timers.delete(id); },
    runDelay(delay) {
      const entry = [...timers.entries()].find(([, timer]) => timer.delay === delay);
      assert.ok(entry, `missing ${delay}ms timer`);
      timers.delete(entry[0]);
      entry[1].callback();
    },
    hasDelay(delay) {
      return [...timers.values()].some((timer) => timer.delay === delay);
    },
  };
}

test("disabled wake word passes PCM through unchanged", () => {
  const controller = new WakeWordController();
  assert.equal(controller.routePcm16(new ArrayBuffer(8)), true);
  assert.equal(controller.state, "off");
});

test("default browser-style timer wrappers connect without an illegal receiver", async () => {
  const socket = new FakeSocket();
  const controller = new WakeWordController({ socketFactory: () => socket, urlFactory: () => "ws://local.test" });
  controller.configure({ selected: true, configured: true, healthy: true });
  const connected = controller.connect();
  socket.message({ type: "ready", phrase: "Hey Eva" });
  assert.equal(await connected, true);
  controller.disconnect();
});

test("wake follow-up defaults to 60 seconds and pauses while busy", async () => {
  const socket = new FakeSocket();
  const scheduler = fakeScheduler();
  const controller = new WakeWordController({
    socketFactory: () => socket,
    urlFactory: () => "ws://local.test/api/wake-word/stream",
    ...scheduler,
  });
  controller.configure({ selected: true, configured: true, healthy: true });
  const connected = controller.connect();
  socket.message({ type: "ready", phrase: "Hey Eva" });
  assert.equal(await connected, true);
  controller.manualWake();
  assert.equal(scheduler.hasDelay(60_000), true);
  controller.setBusy(true);
  assert.equal(scheduler.hasDelay(60_000), false);
  controller.setBusy(false);
  assert.equal(scheduler.hasDelay(60_000), true);
});

test("sleeping PCM goes only to the detector, then passes after detection", async () => {
  const socket = new FakeSocket();
  const scheduler = fakeScheduler();
  const controller = new WakeWordController({
    socketFactory: () => socket,
    urlFactory: () => "ws://local.test/api/wake-word/stream",
    ...scheduler,
  });
  controller.configure({ selected: true, configured: true, healthy: true, phrase: "Hey Eva", followupMs: 20_000 });
  const connected = controller.connect();
  socket.message({ type: "ready", phrase: "Hey Eva" });
  assert.equal(await connected, true);
  assert.equal(controller.state, "sleeping");

  const sleepingChunk = new ArrayBuffer(16);
  assert.equal(controller.routePcm16(sleepingChunk), false);
  assert.deepEqual(socket.sent, [sleepingChunk]);

  socket.message({ type: "detected", phrase: "HEY EVA" });
  assert.equal(controller.state, "heard");
  assert.equal(controller.routePcm16(new ArrayBuffer(16)), false);
  scheduler.runDelay(220);
  assert.equal(controller.state, "awake");
  assert.equal(controller.routePcm16(new ArrayBuffer(16)), true);
  assert.equal(scheduler.hasDelay(20_000), true);
  controller.setBusy(true);
  assert.equal(scheduler.hasDelay(20_000), false);
  controller.setBusy(false);
  scheduler.runDelay(20_000);
  assert.equal(controller.state, "sleeping");
});

test("detector failure stays fail closed while manual wake remains available", async () => {
  const socket = new FakeSocket();
  const controller = new WakeWordController({ socketFactory: () => socket, urlFactory: () => "ws://local.test" });
  controller.configure({ selected: true, configured: true, healthy: true });
  const connected = controller.connect();
  socket.dispatchEvent(new Event("error"));
  assert.equal(await connected, false);
  assert.equal(controller.state, "unavailable");
  assert.equal(controller.routePcm16(new ArrayBuffer(8)), false);
  controller.manualWake();
  assert.equal(controller.state, "awake");
  assert.equal(controller.routePcm16(new ArrayBuffer(8)), true);
  controller.setBusy(true);
  controller.setBusy(false);
  assert.equal(controller.state, "unavailable");
  controller.disconnect();
});

test("S2S mic router consumes sleeping audio before encoding or send", () => {
  const client = new S2sWsRealtimeClient({
    directUrl: "ws://local.test/v1/realtime",
    micChunkRouter: () => false,
  });
  client._ws = { readyState: 1 };
  client._sessionConfigured = true;
  client._muted = false;
  let sends = 0;
  client._send = () => { sends += 1; };
  client._onMicChunk(new ArrayBuffer(32));
  assert.equal(sends, 0);
});

test("wake-relevant client status stays busy until queued speaker audio drains", async () => {
  const client = new S2sWsRealtimeClient({ directUrl: "ws://local.test/v1/realtime" });
  client._status = "ai-speaking";
  client._openResponses = 1;
  client._playbackActive = true;

  await client._onWsMessage(JSON.stringify({
    type: "response.done",
    response: { id: "resp_1", status: "completed" },
  }));
  assert.equal(client.status, "ai-speaking");
  assert.equal(client._responseDoneWaitingForPlayback, true);

  client._onPlaybackMessage({ kind: "underrun" });
  assert.equal(client.status, "connected");
  assert.equal(client._responseDoneWaitingForPlayback, false);
});

test("cancelled old response does not release wake while new user speech is active", async () => {
  const client = new S2sWsRealtimeClient({ directUrl: "ws://local.test/v1/realtime" });
  client._status = "ai-speaking";
  client._openResponses = 1;
  client._playbackActive = true;

  await client._onWsMessage(JSON.stringify({ type: "input_audio_buffer.speech_started" }));
  await client._onWsMessage(JSON.stringify({
    type: "response.done",
    response: { id: "resp_old", status: "cancelled" },
  }));
  assert.equal(client.status, "user-speaking");
});

test("HTTPS proxy preserves the wake word WebSocket upgrade", () => {
  const nginx = readFileSync(new URL("../deploy/local-https/nginx.conf", import.meta.url), "utf8");
  const location = nginx.match(/location = \/api\/wake-word\/stream \{([\s\S]*?)\n    \}/)?.[1];
  assert.ok(location, "missing exact wake word WebSocket location");
  assert.match(location, /proxy_http_version 1\.1;/);
  assert.match(location, /proxy_set_header Upgrade \$http_upgrade;/);
  assert.match(location, /proxy_set_header Connection \$connection_upgrade;/);
});

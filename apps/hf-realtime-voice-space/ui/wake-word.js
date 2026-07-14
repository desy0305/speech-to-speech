// @ts-check

/** @typedef {"off" | "sleeping" | "heard" | "awake" | "unavailable"} WakeState */

/**
 * Routes the existing 16 kHz PCM stream either to the local keyword detector
 * or to the realtime S2S socket. The default/disabled path always passes audio
 * through unchanged.
 */
export class WakeWordController extends EventTarget {
  /**
   * @param {{
   *   socketFactory?: (url: string) => WebSocket,
   *   urlFactory?: () => string,
   *   setTimeoutFn?: typeof setTimeout,
   *   clearTimeoutFn?: typeof clearTimeout,
   * }} [options]
   */
  constructor(options = {}) {
    super();
    this._socketFactory = options.socketFactory || ((url) => new WebSocket(url));
    this._urlFactory = options.urlFactory || (() => {
      const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
      return `${scheme}//${window.location.host}/api/wake-word/stream`;
    });
    this._setTimeout = options.setTimeoutFn || ((callback, delay) => globalThis.setTimeout(callback, delay));
    this._clearTimeout = options.clearTimeoutFn || ((timer) => globalThis.clearTimeout(timer));
    this._socket = null;
    this._selected = false;
    this._configured = false;
    this._healthy = false;
    this._phrase = "Hey Eva";
    this._followupMs = 20_000;
    this._state = /** @type {WakeState} */ ("off");
    this._busy = false;
    this._detectorReady = false;
    this._closing = false;
    this._pendingUnavailable = false;
    this._sleepTimer = 0;
    this._ackTimer = 0;
  }

  get state() { return this._state; }
  get phrase() { return this._phrase; }
  get selected() { return this._selected; }
  get detectorReady() { return this._detectorReady; }
  get shouldGate() { return this._selected && this._configured; }

  /**
   * @param {{ selected?: boolean; configured?: boolean; healthy?: boolean; phrase?: string; followupMs?: number }} config
   */
  configure(config) {
    this._selected = !!config.selected;
    this._configured = !!config.configured;
    this._healthy = config.healthy !== false;
    this._phrase = String(config.phrase || "Hey Eva").trim() || "Hey Eva";
    const followup = Number(config.followupMs);
    this._followupMs = Number.isFinite(followup) ? Math.max(5_000, Math.min(120_000, followup)) : 20_000;
    if (!this.shouldGate) {
      this.disconnect();
      this._setState("off");
    } else if (!this._healthy && !this._socket) {
      this._setState("unavailable");
    }
  }

  async connect() {
    this.disconnect({ preserveState: true });
    if (!this.shouldGate) {
      this._setState("off");
      return false;
    }
    this._closing = false;
    this._detectorReady = false;
    this._pendingUnavailable = false;
    this._setState("unavailable");

    return new Promise((resolve) => {
      let settled = false;
      let connectTimer = 0;
      let socket;
      try {
        socket = this._socketFactory(this._urlFactory());
      } catch {
        resolve(false);
        return;
      }
      this._socket = socket;
      socket.binaryType = "arraybuffer";

      const finish = (value) => {
        if (settled) return;
        settled = true;
        if (connectTimer) this._clearTimeout(connectTimer);
        connectTimer = 0;
        resolve(value);
      };
      socket.addEventListener("message", (event) => {
        let data = null;
        try { data = JSON.parse(String(event.data || "{}")); } catch { return; }
        if (data.type === "proxy.ready") return;
        if (data.type === "ready") {
          this._detectorReady = true;
          this._healthy = true;
          this._setState("sleeping");
          finish(true);
          return;
        }
        if (data.type === "detected") {
          this._onDetected(data);
          return;
        }
        if (data.type === "error") {
          this._markUnavailable(String(data.message || "Wake detector error."));
          finish(false);
        }
      });
      socket.addEventListener("error", () => {
        this._markUnavailable("Wake detector connection failed.");
        finish(false);
      });
      socket.addEventListener("close", () => {
        const expected = this._closing;
        this._socket = null;
        this._detectorReady = false;
        if (!expected && this.shouldGate) this._markUnavailable("Wake detector disconnected.");
        finish(false);
      });
      connectTimer = this._setTimeout(() => finish(this._detectorReady), 4_000);
    });
  }

  /**
   * Return true only when the PCM chunk should continue to the S2S backend.
   * @param {ArrayBuffer} pcm16
   */
  routePcm16(pcm16) {
    if (!this.shouldGate) return true;
    if (this._state === "awake") return true;
    if (
      this._state === "sleeping" &&
      this._detectorReady &&
      this._socket &&
      this._socket.readyState === 1
    ) {
      try {
        this._socket.send(pcm16);
      } catch {
        this._markUnavailable("Wake detector send failed.");
      }
    }
    return false;
  }

  manualWake() {
    if (!this.shouldGate) return;
    this._pendingUnavailable = !this._detectorReady;
    this._setState("awake");
    this.touch();
  }

  sleep() {
    this._clearTimers();
    if (!this.shouldGate) {
      this._setState("off");
    } else if (this._detectorReady) {
      this._setState("sleeping");
    } else {
      this._setState("unavailable");
    }
  }

  touch() {
    if (this._state !== "awake" || this._busy) return;
    if (this._sleepTimer) this._clearTimeout(this._sleepTimer);
    this._sleepTimer = this._setTimeout(() => {
      this._sleepTimer = 0;
      this.sleep();
    }, this._followupMs);
  }

  /** @param {boolean} busy */
  setBusy(busy) {
    this._busy = !!busy;
    if (this._busy) {
      if (this._sleepTimer) this._clearTimeout(this._sleepTimer);
      this._sleepTimer = 0;
      return;
    }
    if (this._pendingUnavailable) {
      this._pendingUnavailable = false;
      this._setState("unavailable");
      return;
    }
    this.touch();
  }

  /** @param {{ preserveState?: boolean }} [options] */
  disconnect(options = {}) {
    this._clearTimers();
    this._closing = true;
    this._detectorReady = false;
    if (this._socket) {
      try { this._socket.close(1000, "client closed"); } catch { /* ignored */ }
    }
    this._socket = null;
    if (!options.preserveState) this._setState("off");
  }

  _onDetected(detail) {
    if (this._state !== "sleeping") return;
    this._setState("heard");
    this.dispatchEvent(new CustomEvent("detected", { detail }));
    if (this._ackTimer) this._clearTimeout(this._ackTimer);
    this._ackTimer = this._setTimeout(() => {
      this._ackTimer = 0;
      if (!this.shouldGate) return;
      this._setState("awake");
      this.touch();
    }, 220);
  }

  _markUnavailable(message) {
    this._healthy = false;
    this._detectorReady = false;
    if (this._state === "awake" && this._busy) {
      this._pendingUnavailable = true;
    } else {
      this._setState("unavailable");
    }
    this.dispatchEvent(new CustomEvent("unavailable", { detail: { message } }));
  }

  _clearTimers() {
    if (this._sleepTimer) this._clearTimeout(this._sleepTimer);
    if (this._ackTimer) this._clearTimeout(this._ackTimer);
    this._sleepTimer = 0;
    this._ackTimer = 0;
  }

  /** @param {WakeState} state */
  _setState(state) {
    if (this._state === state) return;
    this._state = state;
    this.dispatchEvent(new CustomEvent("statechange", { detail: { state, phrase: this._phrase } }));
  }
}

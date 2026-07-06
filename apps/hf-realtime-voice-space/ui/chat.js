// @ts-check
/**
 * Conversation surface: history panel, ephemeral bubbles, typed composer,
 * attachment chips, activity feedback, and tool-call progress rows.
 */

import { $, escHtml, DEBUG } from "./dom.js";

const WRENCH_PATH = `<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>`;
const CHAT_BUBBLE_SVG = `<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>`;
const EMPTY_STATE_HTML = `<div id="chat-empty" class="chat-empty">${CHAT_BUBBLE_SVG}<span class="chat-empty-title">No messages yet</span><span class="chat-empty-hint">Tap the orb or type below</span></div>`;

const IMAGE_TYPES = new Set(["image/jpeg", "image/png", "image/webp", "image/gif"]);
const TEXT_EXTS = new Set(["txt", "md", "json", "csv", "log"]);
const MAX_ATTACHMENTS = 6;
const MAX_IMAGE_BYTES = 4 * 1024 * 1024;
const MAX_TEXT_BYTES = 200 * 1024;
const MAX_TEXT_CHARS = 20000;

/** @typedef {{ id: string; kind: "image" | "text"; name: string; type: string; size: number; dataUrl?: string; text?: string }} ChatAttachment */

/** @param {number} bytes */
function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 10 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

/** @param {string} name */
function extension(name) {
  const idx = name.lastIndexOf(".");
  return idx >= 0 ? name.slice(idx + 1).toLowerCase() : "";
}

/** @param {File} file */
function isTextFile(file) {
  return file.type.startsWith("text/") || file.type === "application/json" || TEXT_EXTS.has(extension(file.name));
}

/** @param {File} file @returns {Promise<string>} */
function readAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(reader.error || new Error(`Could not read ${file.name}`));
    reader.readAsDataURL(file);
  });
}

/** @param {File} file @returns {Promise<string>} */
function readAsText(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(reader.error || new Error(`Could not read ${file.name}`));
    reader.readAsText(file);
  });
}

export class ChatView extends EventTarget {
  constructor() {
    super();
    /** @type {HTMLButtonElement} */
    this._chatBtn = $("#chat-btn");
    /** @type {HTMLSpanElement} */
    this._chatBadge = $("#chat-badge");
    /** @type {HTMLDivElement} */
    this._chatPanel = $("#chat-panel");
    /** @type {HTMLDivElement} */
    this._chatPanelBackdrop = $("#chat-panel-backdrop");
    /** @type {HTMLButtonElement} */
    this._chatPanelClose = $("#chat-panel-close");
    /** @type {HTMLDivElement} */
    this._chatHistory = $("#chat-history");
    /** @type {HTMLDivElement} */
    this._bubbleStack = $("#bubble-stack");
    /** @type {HTMLDivElement} */
    this._activity = $("#chat-activity");
    /** @type {HTMLSpanElement} */
    this._activityText = $("#chat-activity-text");
    /** @type {HTMLDivElement} */
    this._attachmentBar = $("#chat-attachments");
    /** @type {HTMLDivElement} */
    this._dropZone = $("#chat-drop-zone");
    /** @type {HTMLTextAreaElement} */
    this._input = $("#chat-input");
    /** @type {HTMLButtonElement} */
    this._sendBtn = $("#chat-send");
    /** @type {HTMLButtonElement} */
    this._attachBtn = $("#chat-attach");
    /** @type {HTMLButtonElement} */
    this._clearDraftBtn = $("#chat-clear-draft");
    /** @type {HTMLButtonElement} */
    this._clearHistoryBtn = $("#chat-clear-history");
    /** @type {HTMLButtonElement} */
    this._stopBtn = $("#chat-stop-generation");
    /** @type {HTMLButtonElement} */
    this._cameraBtn = $("#chat-camera-attach");
    /** @type {HTMLInputElement} */
    this._fileInput = $("#chat-file-input");
    /** @type {HTMLInputElement} */
    this._speakReplies = $("#chat-speak-replies");
    /** @type {HTMLElement} */
    this._error = $("#chat-composer-error");
    /** @type {HTMLElement} */
    this._runtime = $("#chat-runtime");
    /** @type {HTMLElement} */
    this._runtimeLlm = $("#chat-runtime-llm");
    /** @type {HTMLElement} */
    this._runtimeStt = $("#chat-runtime-stt");
    /** @type {HTMLElement} */
    this._runtimeTts = $("#chat-runtime-tts");

    this._panelOpen = false;
    this._scrollQueued = false;
    /** @type {Map<string, HTMLElement>} */
    this._userHistByItem = new Map();
    /** @type {HTMLElement | null} */
    this._activeUserBubble = null;
    this._activeUserItemId = "";
    this._anonSeq = 0;
    /** @type {Map<string, { bubble: HTMLElement, hist: HTMLElement }>} */
    this._asstByResp = new Map();
    /** @type {WeakMap<HTMLElement, number>} */
    this._bubbleExpiry = new WeakMap();
    this._reaperHandle = 0;
    /** @type {ChatAttachment[]} */
    this._attachments = [];
    /** @type {Map<string, HTMLElement>} */
    this._toolRowsByCallId = new Map();

    this.renderEmptyState();
    this._syncComposer();
    this._wirePanel();
    this._wireComposer();
  }

  _wirePanel() {
    this._chatBtn.addEventListener("click", () => (this._panelOpen ? this._closePanel() : this._openPanel()));
    this._chatPanelClose.addEventListener("click", () => this._closePanel());
    this._chatPanelBackdrop.addEventListener("click", () => this._closePanel());
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && this._panelOpen) this._closePanel();
    });
  }

  _wireComposer() {
    this._input.addEventListener("input", () => {
      this._autoSizeInput();
      this._syncComposer();
    });
    this._input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        this._submitComposer();
      }
    });
    this._sendBtn.addEventListener("click", () => this._submitComposer());
    this._attachBtn.addEventListener("click", () => this._fileInput.click());
    this._clearDraftBtn.addEventListener("click", () => this.clearDraft());
    this._clearHistoryBtn.addEventListener("click", () => {
      this.clear();
      this.reset({ dismiss: true });
      this.setActivity("idle", "Chat cleared");
      this.dispatchEvent(new CustomEvent("clear-chat"));
    });
    this._stopBtn.addEventListener("click", () => this.dispatchEvent(new CustomEvent("stop-generation")));
    this._cameraBtn.addEventListener("click", () => this.dispatchEvent(new CustomEvent("camera-snapshot")));
    this._speakReplies.addEventListener("change", () => {
      this.dispatchEvent(new CustomEvent("speak-replies-change", { detail: { enabled: this._speakReplies.checked } }));
    });
    this._fileInput.addEventListener("change", () => {
      void this._handleFiles(Array.from(this._fileInput.files || []));
      this._fileInput.value = "";
    });
    this._input.addEventListener("paste", (e) => {
      const files = Array.from(e.clipboardData?.files || []);
      if (!files.length) return;
      e.preventDefault();
      void this._handleFiles(files);
    });
    for (const type of ["dragenter", "dragover"]) {
      this._dropZone.addEventListener(type, (e) => {
        e.preventDefault();
        this._dropZone.classList.add("drag-over");
      });
    }
    for (const type of ["dragleave", "drop"]) {
      this._dropZone.addEventListener(type, (e) => {
        e.preventDefault();
        this._dropZone.classList.remove("drag-over");
      });
    }
    this._dropZone.addEventListener("drop", (e) => {
      void this._handleFiles(Array.from(e.dataTransfer?.files || []));
    });
  }

  _openPanel() {
    this._panelOpen = true;
    this._chatPanel.classList.add("open");
    this._chatBadge.classList.remove("visible");
    this._scrollToBottom();
    setTimeout(() => this._input.focus({ preventScroll: true }), 80);
  }

  _closePanel() {
    this._panelOpen = false;
    this._chatPanel.classList.remove("open");
  }

  _scrollToBottom() {
    if (!this._panelOpen || this._scrollQueued) return;
    this._scrollQueued = true;
    requestAnimationFrame(() => {
      this._scrollQueued = false;
      this._chatHistory.scrollTop = this._chatHistory.scrollHeight;
    });
  }

  _markUnread() {
    if (this._panelOpen) {
      this._scrollToBottom();
      return;
    }
    this._chatBadge.classList.add("visible");
  }

  /** @param {{ container: string, prefix: string, role: "user"|"assistant"|"system", text: string, partial?: boolean, error?: boolean }} o */
  _buildMessageEl({ container, prefix, role, text, partial = false, error = false }) {
    const el = document.createElement("div");
    el.className = `${container} ${role}${error ? " error" : ""}`;
    const label = role === "user" ? "You" : role === "assistant" ? "Assistant" : "System";
    const roleEl = document.createElement("div");
    roleEl.className = `${prefix}-role`;
    roleEl.textContent = label;
    const body = document.createElement("div");
    body.className = `${prefix}-body${partial ? " partial" : ""}`;
    body.textContent = text;
    el.append(roleEl, body);
    return el;
  }

  /** @param {"user"|"assistant"|"tool"} role @param {string} text */
  _spawnBubble(role, text) {
    let el;
    if (role === "tool") {
      el = document.createElement("div");
      el.className = "bubble tool";
      el.innerHTML = `<svg class="bubble-tool-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${WRENCH_PATH}</svg><span class="bubble-tool-text">${escHtml(text)}</span>`;
    } else {
      el = this._buildMessageEl({ container: "bubble", prefix: "bubble", role, text });
    }
    this._bubbleStack.appendChild(el);
    const visible = /** @type {HTMLElement[]} */ ([...this._bubbleStack.querySelectorAll(".bubble:not(.out)")]);
    if (visible.length > 3) {
      this._dismissBubble(visible.find((b) => b !== this._activeUserBubble) ?? visible[0]);
    }
    requestAnimationFrame(() => el.classList.add("in"));
    return el;
  }

  /** @param {HTMLElement} el @param {string} text */
  _updateBubbleText(el, text) {
    const t = el.querySelector(".bubble-body");
    if (t) t.textContent = text;
  }

  /** @param {HTMLElement} el */
  _dismissBubble(el) {
    if (!el || el.classList.contains("out")) return;
    this._bubbleExpiry.delete(el);
    el.classList.remove("in");
    el.classList.add("out");
    const remove = () => el.remove();
    el.addEventListener("transitionend", remove, { once: true });
    setTimeout(remove, 400);
  }

  _reapBubbles() {
    this._reaperHandle = 0;
    const now = Date.now();
    const visible = /** @type {HTMLElement[]} */ ([...this._bubbleStack.querySelectorAll(".bubble:not(.out)")]);
    let nextWake = Infinity;
    for (const el of visible) {
      const exp = this._bubbleExpiry.get(el) ?? now;
      if (exp <= now) {
        this._dismissBubble(el);
      } else {
        nextWake = exp;
        break;
      }
    }
    if (nextWake !== Infinity) {
      this._reaperHandle = window.setTimeout(() => this._reapBubbles(), Math.max(50, nextWake - Date.now()));
    }
  }

  /** @param {HTMLElement} el @param {number} [delay] */
  _bumpDismiss(el, delay = 4000) {
    this._bubbleExpiry.set(el, Date.now() + delay);
    if (!this._reaperHandle) this._reaperHandle = window.setTimeout(() => this._reapBubbles(), delay);
  }

  renderEmptyState() {
    this._chatHistory.innerHTML = EMPTY_STATE_HTML;
  }

  clear() {
    this.renderEmptyState();
    this._chatBadge.classList.remove("visible");
  }

  /** @param {"user"|"assistant"|"system"} role @param {string} text @param {boolean} partial @param {boolean} [error] */
  _appendHistMsg(role, text, partial, error = false) {
    const empty = this._chatHistory.querySelector(".chat-empty");
    if (empty) empty.remove();
    const el = this._buildMessageEl({ container: "hist-msg", prefix: "hist", role, text, partial, error });
    this._chatHistory.appendChild(el);
    this._scrollToBottom();
    return el;
  }

  /** @param {HTMLElement | null} el @param {string} text @param {boolean} partial */
  _updateHistMsg(el, text, partial) {
    if (!el) return;
    const body = /** @type {HTMLElement | null} */ (el.querySelector(".hist-body"));
    if (!body) return;
    body.textContent = text;
    body.classList.toggle("partial", partial);
    this._scrollToBottom();
  }

  /** @param {string} argsJson */
  _formatArgs(argsJson) {
    try { return JSON.stringify(JSON.parse(argsJson || "{}"), null, 2); } catch { return argsJson || "{}"; }
  }

  /** @param {string} name @param {string} argsJson @param {string} output @param {"running"|"completed"|"failed"} status */
  _appendHistTool(name, argsJson, output, status = "completed") {
    const empty = this._chatHistory.querySelector(".chat-empty");
    if (empty) empty.remove();
    const pretty = this._formatArgs(argsJson);
    const el = document.createElement("div");
    el.className = `hist-msg tool ${status}`;
    el.innerHTML = `
      <div class="hist-role">Tool call</div>
      <button class="hist-tool-header" aria-expanded="${status === "running" ? "true" : "false"}">
        <svg class="hist-tool-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${WRENCH_PATH}</svg>
        <span class="hist-tool-name">${escHtml(name)}</span>
        <span class="hist-tool-status">${status}</span>
        <svg class="hist-tool-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>
      </button>
      <div class="hist-tool-body${status === "running" ? " open" : ""}">
        <div class="hist-tool-label">Input</div>
        <div class="hist-tool-block">${escHtml(pretty)}</div>
        <div class="hist-tool-label">Output</div>
        <div class="hist-tool-block hist-tool-output">${escHtml(output || (status === "running" ? "Running..." : "(no output)"))}</div>
      </div>
    `;
    const header = /** @type {HTMLButtonElement} */ (el.querySelector(".hist-tool-header"));
    const body = /** @type {HTMLDivElement} */ (el.querySelector(".hist-tool-body"));
    header.addEventListener("click", () => {
      const expanded = header.getAttribute("aria-expanded") === "true";
      header.setAttribute("aria-expanded", String(!expanded));
      body.classList.toggle("open", !expanded);
    });
    this._chatHistory.appendChild(el);
    this._scrollToBottom();
    return el;
  }

  /** @param {HTMLElement} el @param {string} output @param {"completed"|"failed"} status */
  _updateHistTool(el, output, status) {
    el.classList.remove("running", "completed", "failed");
    el.classList.add(status);
    const statusEl = el.querySelector(".hist-tool-status");
    if (statusEl) statusEl.textContent = status;
    const out = el.querySelector(".hist-tool-output");
    if (out) out.textContent = output || "(no output)";
    this._scrollToBottom();
  }

  /** @param {HTMLElement | null} hist */
  _markHistInterrupted(hist) {
    if (!hist || hist.querySelector(".hist-note")) return;
    hist.classList.add("interrupted");
    const note = document.createElement("div");
    note.className = "hist-note";
    note.textContent = "Interrupted";
    hist.appendChild(note);
  }

  /** @param {string} dataUrl */
  _appendHistImage(dataUrl) {
    const empty = this._chatHistory.querySelector(".chat-empty");
    if (empty) empty.remove();
    const el = document.createElement("div");
    el.className = "hist-msg tool";
    el.innerHTML = `<div class="hist-role">Snapshot</div><img class="hist-image" alt="Webcam snapshot sent to the model" />`;
    const img = /** @type {HTMLImageElement} */ (el.querySelector("img"));
    img.src = dataUrl;
    this._chatHistory.appendChild(el);
    this._scrollToBottom();
  }

  /** @param {{ dismiss?: boolean }} [opts] */
  reset(opts) {
    if (opts?.dismiss) {
      if (this._activeUserBubble) this._dismissBubble(this._activeUserBubble);
      for (const { bubble } of this._asstByResp.values()) this._dismissBubble(bubble);
    }
    this._userHistByItem.clear();
    this._activeUserBubble = null;
    this._activeUserItemId = "";
    this._asstByResp.clear();
    this._toolRowsByCallId.clear();
  }

  /** @param {"idle"|"active"|"connecting"|"processing"|"tool"|"speaking"|"error"} state @param {string} text */
  setActivity(state, text) {
    this._activity.dataset.state = state;
    this._activityText.textContent = text;
  }

  /** @param {boolean} enabled */
  setSpeakReplies(enabled) {
    this._speakReplies.checked = !!enabled;
  }

  get speakReplies() {
    return this._speakReplies.checked;
  }

  /** @param {{ llmProvider?: string; llmModel?: string; stt?: string; tts?: string; activeBackend?: string }} stack */
  setRuntime(stack) {
    const llm = [stack.llmProvider, stack.llmModel].filter(Boolean).join(" / ");
    this._runtimeLlm.textContent = llm || "-";
    this._runtimeStt.textContent = stack.stt || "-";
    this._runtimeTts.textContent = stack.tts || "-";
    this._runtime.hidden = !(llm || stack.stt || stack.tts || stack.activeBackend);
  }

  /** @param {string} message */
  setComposerError(message) {
    this._error.textContent = message;
    this._error.hidden = !message;
    if (message) this.setActivity("error", message);
  }

  _autoSizeInput() {
    this._input.style.height = "auto";
    this._input.style.height = `${Math.min(this._input.scrollHeight, 132)}px`;
  }

  _syncComposer() {
    const hasText = this._input.value.trim().length > 0;
    this._sendBtn.disabled = !(hasText || this._attachments.length > 0);
    this._clearDraftBtn.disabled = !(hasText || this._attachments.length > 0);
  }

  clearDraft() {
    this._input.value = "";
    this._attachments = [];
    this._autoSizeInput();
    this._renderAttachments();
    this._syncComposer();
    this.setComposerError("");
  }

  /** @param {File[]} files */
  async _handleFiles(files) {
    if (!files.length) return;
    this.setComposerError("");
    for (const file of files) {
      if (this._attachments.length >= MAX_ATTACHMENTS) {
        this.setComposerError(`Attachment limit is ${MAX_ATTACHMENTS} files.`);
        break;
      }
      try {
        await this._addFile(file);
      } catch (err) {
        this.setComposerError(err instanceof Error ? err.message : String(err));
      }
    }
    this._renderAttachments();
    this._syncComposer();
  }

  /** @param {File} file */
  async _addFile(file) {
    if (IMAGE_TYPES.has(file.type)) {
      if (file.size > MAX_IMAGE_BYTES) throw new Error(`${file.name} is too large. Images are limited to ${formatBytes(MAX_IMAGE_BYTES)}.`);
      this._attachments.push({
        id: `att-${Date.now()}-${Math.random().toString(16).slice(2)}`,
        kind: "image",
        name: file.name || "image",
        type: file.type,
        size: file.size,
        dataUrl: await readAsDataUrl(file),
      });
      return;
    }
    if (isTextFile(file)) {
      if (file.size > MAX_TEXT_BYTES) throw new Error(`${file.name} is too large. Text files are limited to ${formatBytes(MAX_TEXT_BYTES)}.`);
      const raw = await readAsText(file);
      const text = raw.length > MAX_TEXT_CHARS ? `${raw.slice(0, MAX_TEXT_CHARS)}\n[truncated]` : raw;
      this._attachments.push({
        id: `att-${Date.now()}-${Math.random().toString(16).slice(2)}`,
        kind: "text",
        name: file.name || "attachment.txt",
        type: file.type || "text/plain",
        size: file.size,
        text,
      });
      return;
    }
    throw new Error(`${file.name || "File"} is not supported.`);
  }

  /** @param {string} dataUrl @param {string} [name] */
  addImageAttachment(dataUrl, name = "camera-snapshot.jpg") {
    if (!dataUrl) {
      this.setComposerError("Camera is not ready.");
      return;
    }
    if (this._attachments.length >= MAX_ATTACHMENTS) {
      this.setComposerError(`Attachment limit is ${MAX_ATTACHMENTS} files.`);
      return;
    }
    const type = dataUrl.slice(5, dataUrl.indexOf(";")) || "image/jpeg";
    this._attachments.push({
      id: `att-${Date.now()}-${Math.random().toString(16).slice(2)}`,
      kind: "image",
      name,
      type,
      size: Math.round(dataUrl.length * 0.75),
      dataUrl,
    });
    this._renderAttachments();
    this._syncComposer();
    this.setActivity("active", "Snapshot attached");
  }

  _renderAttachments() {
    this._attachmentBar.innerHTML = "";
    this._attachmentBar.hidden = this._attachments.length === 0;
    for (const att of this._attachments) {
      const chip = document.createElement("div");
      chip.className = "chat-attachment";
      const preview = att.kind === "image" && att.dataUrl
        ? `<img class="chat-attachment-preview" alt="" src="${escHtml(att.dataUrl)}" />`
        : `<span class="chat-attachment-preview" aria-hidden="true">TXT</span>`;
      chip.innerHTML = `
        ${preview}
        <span class="chat-attachment-meta">
          <span class="chat-attachment-name">${escHtml(att.name)}</span>
          <span class="chat-attachment-info">${escHtml(att.kind)} &middot; ${escHtml(formatBytes(att.size))}</span>
        </span>
        <button class="chat-attachment-remove" type="button" aria-label="Remove attachment">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>
        </button>
      `;
      const btn = /** @type {HTMLButtonElement} */ (chip.querySelector("button"));
      btn.addEventListener("click", () => {
        this._attachments = this._attachments.filter((item) => item.id !== att.id);
        this._renderAttachments();
        this._syncComposer();
      });
      this._attachmentBar.appendChild(chip);
    }
  }

  _submitComposer() {
    const text = this._input.value.trim();
    if (!text && !this._attachments.length) return;
    /** @type {ChatAttachment[]} */
    const attachments = this._attachments.map((att) => ({ ...att }));
    this.dispatchEvent(new CustomEvent("send-message", {
      detail: {
        text,
        attachments,
        images: attachments.filter((att) => att.kind === "image"),
        textAttachments: attachments.filter((att) => att.kind === "text"),
        speakReplies: this.speakReplies,
      },
    }));
    this.clearDraft();
  }

  /** @param {string} text @param {ChatAttachment[]} [attachments] */
  onLocalUserMessage(text, attachments = []) {
    const display = text || (attachments.length ? "(attachment)" : "");
    const hist = this._appendHistMsg("user", display, false);
    this._appendAttachmentSummary(hist, attachments);
    const bubble = this._spawnBubble("user", display || "Attachment sent");
    this._bumpDismiss(bubble, 5000);
    this._markUnread();
  }

  /** @param {HTMLElement} hist @param {ChatAttachment[]} attachments */
  _appendAttachmentSummary(hist, attachments) {
    if (!attachments.length) return;
    const wrap = document.createElement("div");
    wrap.className = "hist-attachments";
    for (const att of attachments) {
      const chip = document.createElement("span");
      chip.className = "hist-attachment";
      if (att.kind === "image" && att.dataUrl) {
        chip.innerHTML = `<img alt="" src="${escHtml(att.dataUrl)}" /><span>${escHtml(att.name)}</span>`;
      } else {
        chip.textContent = `${att.name} (${formatBytes(att.size)})`;
      }
      wrap.appendChild(chip);
    }
    hist.appendChild(wrap);
  }

  /** @param {string} message */
  onServerError(message) {
    this._appendHistMsg("system", message, false, true);
    this.setActivity("error", message);
    this._markUnread();
  }

  /** @param {number} pending */
  onQueuedResponse(pending) {
    this.setActivity("processing", pending > 1 ? `${pending} responses queued` : "Response queued");
  }

  /** @param {{ role: "user" | "assistant"; text: string; partial: boolean; itemId?: string; responseId?: string }} d */
  onTranscript(d) {
    if (DEBUG) console.debug(`[ui] transcript role=${d.role} partial=${d.partial} item=${d.itemId} resp=${d.responseId} text=${JSON.stringify(d.text)}`);

    if (d.role === "user") {
      const id = d.itemId || this._activeUserItemId || `_u${++this._anonSeq}`;
      let hist = this._userHistByItem.get(id);
      if (!hist) {
        hist = this._appendHistMsg("user", d.text, d.partial);
        this._userHistByItem.set(id, hist);
      } else {
        this._updateHistMsg(hist, d.text, d.partial);
      }

      if (this._activeUserItemId !== id || !this._activeUserBubble) {
        this._activeUserBubble = this._spawnBubble("user", d.text);
        this._activeUserItemId = id;
      } else {
        this._updateBubbleText(this._activeUserBubble, d.text);
      }
      this._bumpDismiss(this._activeUserBubble, 6000);
      this.setActivity(d.partial ? "active" : "processing", d.partial ? "Transcribing" : "Thinking");
      this._markUnread();
      return;
    }

    const rid = d.responseId || `_a${++this._anonSeq}`;
    const entry = this._asstByResp.get(rid);
    if (!entry) {
      const bubble = this._spawnBubble("assistant", d.text);
      this._asstByResp.set(rid, { bubble, hist: this._appendHistMsg("assistant", d.text, d.partial) });
      this._bumpDismiss(bubble);
    } else {
      this._updateBubbleText(entry.bubble, d.text);
      this._updateHistMsg(entry.hist, d.text, d.partial);
      this._bumpDismiss(entry.bubble);
    }
    this.setActivity(d.partial ? "processing" : "speaking", d.partial ? "Assistant typing" : "Assistant replied");
    this._markUnread();
  }

  /** @param {{ responseId: string; status: string; audible?: boolean; transcript?: string }} detail */
  onResponseFinished(detail) {
    const { responseId, status, transcript } = detail;
    if (DEBUG) console.debug(`[ui] response-finished resp=${responseId} status=${status} known=${this._asstByResp.has(responseId)}`);
    if (!responseId) {
      this.setActivity(status === "completed" ? "idle" : "error", status === "completed" ? "Done" : `Response ${status}`);
      return;
    }
    const entry = this._asstByResp.get(responseId);

    if (status === "cancelled") {
      let hist = entry?.hist ?? null;
      if (!hist && transcript) {
        hist = this._appendHistMsg("assistant", transcript, false);
      } else if (hist && transcript) {
        this._updateHistMsg(hist, transcript, false);
      }
      if (hist) this._markHistInterrupted(hist);
      this._asstByResp.delete(responseId);
      this.setActivity("idle", "Stopped");
      return;
    }

    this._asstByResp.delete(responseId);
    this.setActivity(status === "completed" ? "idle" : "error", status === "completed" ? "Done" : `Response ${status}`);
  }

  /** @param {string} name @param {string} [argsJson] @param {string} [callId] */
  onToolCall(name, argsJson = "{}", callId = "") {
    this._bumpDismiss(this._spawnBubble("tool", name));
    const row = this._appendHistTool(name, argsJson, "Running...", "running");
    if (callId) this._toolRowsByCallId.set(callId, row);
    this.setActivity("tool", `Running ${name}`);
    this._markUnread();
  }

  /** @param {string} name @param {string} argsJson @param {string} output @param {string} [image] @param {string} [callId] */
  onToolResult(name, argsJson, output, image, callId = "") {
    const status = output.startsWith("Tool failed:") ? "failed" : "completed";
    const row = callId ? this._toolRowsByCallId.get(callId) : null;
    if (row) {
      this._updateHistTool(row, output, status);
      this._toolRowsByCallId.delete(callId);
    } else {
      this._appendHistTool(name, argsJson, output, status);
    }
    if (image) this._appendHistImage(image);
    this.setActivity(status === "failed" ? "error" : "processing", status === "failed" ? `${name} failed` : `${name} completed`);
    this._markUnread();
  }
}

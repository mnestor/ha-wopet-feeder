// Wopet feeder voice-clip recorder card. Loaded on every dashboard by the wopet_local
// integration; add it with `type: custom:wopet-clip-card`.
const MAX_SECONDS = 10;
const SLOTS = [1, 2, 3, 4, 5];

class WopetClipCard extends HTMLElement {
  static getStubConfig() {
    return { title: "Feeder voice clips" };
  }

  setConfig(config) {
    this._config = config || {};
    this._slot = this._slot || 1;
    this._label = this._label || "";
    if (this.shadowRoot) this._render();
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) {
      this._render();
      this._loadClips();
    }
  }

  getCardSize() {
    return 4;
  }

  _query(extra) {
    const q = new URLSearchParams(extra);
    if (this._config.entry_id) q.set("entry_id", this._config.entry_id);
    return q.toString();
  }

  async _loadClips() {
    try {
      const res = await this._hass.callApi("GET", `wopet_local/clips?${this._query({})}`);
      this._clips = res.clips || [];
    } catch (err) {
      this._clips = [];
    }
    this._renderSlots();
  }

  _slotName(n) {
    const clip = (this._clips || []).find((c) => c.clip === n - 1);
    if (!clip || clip.empty) return `Clip ${n} (empty)`;
    return `Clip ${n}: ${clip.label} (${clip.seconds} s)`;
  }

  _render() {
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    const title = this._config.title || "Feeder voice clips";
    this.shadowRoot.innerHTML = `
      <style>
        ha-card { padding: 16px; }
        h2 { margin: 0 0 12px; font-size: 1.2em; font-weight: 500; }
        label { display: block; font-size: 0.85em; color: var(--secondary-text-color); margin: 8px 0 4px; }
        select, input { width: 100%; box-sizing: border-box; padding: 8px; font-size: 1em;
          border: 1px solid var(--divider-color); border-radius: 6px;
          background: var(--card-background-color); color: var(--primary-text-color); }
        .row { display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; }
        button { flex: 1; min-width: 120px; padding: 12px; font-size: 1em; border: none;
          border-radius: 8px; cursor: pointer; color: var(--text-primary-color, #fff);
          background: var(--primary-color); }
        button.rec { background: var(--error-color, #db4437); }
        button.secondary { background: var(--secondary-background-color);
          color: var(--primary-text-color); border: 1px solid var(--divider-color); }
        button:disabled { opacity: 0.45; cursor: default; }
        audio { width: 100%; margin-top: 12px; }
        .status { margin-top: 10px; min-height: 1.3em; font-size: 0.9em; color: var(--secondary-text-color); }
        .status.err { color: var(--error-color, #db4437); }
      </style>
      <ha-card>
        <h2>${title}</h2>
        <label for="slot">Clip slot</label>
        <select id="slot"></select>
        <label for="label">Name (optional)</label>
        <input id="label" maxlength="31" placeholder="Dinner bell">
        <div class="row">
          <button id="rec">Record</button>
        </div>
        <audio id="preview" controls hidden></audio>
        <div class="row">
          <button id="save" disabled>Save to feeder</button>
          <button id="play" class="secondary">Play on feeder</button>
        </div>
        <div class="status" id="status"></div>
      </ha-card>`;
    const $ = (id) => this.shadowRoot.getElementById(id);
    $("slot").addEventListener("change", (e) => { this._slot = Number(e.target.value); });
    $("label").value = this._label;
    $("label").addEventListener("input", (e) => { this._label = e.target.value; });
    $("rec").addEventListener("click", () => (this._recorder ? this._stop() : this._start()));
    $("save").addEventListener("click", () => this._save());
    $("play").addEventListener("click", () => this._play());
    this._renderSlots();
  }

  _renderSlots() {
    const sel = this.shadowRoot && this.shadowRoot.getElementById("slot");
    if (!sel) return;
    sel.innerHTML = SLOTS.map((n) =>
      `<option value="${n}" ${n === this._slot ? "selected" : ""}>${this._esc(this._slotName(n))}</option>`
    ).join("");
  }

  _esc(text) {
    return String(text).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  }

  _status(text, error = false) {
    const el = this.shadowRoot.getElementById("status");
    el.textContent = text;
    el.classList.toggle("err", error);
  }

  async _start() {
    if (!navigator.mediaDevices || !window.MediaRecorder) {
      this._status("No microphone here: open Home Assistant over HTTPS or in the Companion app.", true);
      return;
    }
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch (err) {
      this._status(`Microphone blocked: ${err.message || err}`, true);
      return;
    }
    const types = ["audio/webm;codecs=opus", "audio/mp4", "audio/ogg;codecs=opus", "audio/webm"];
    const mimeType = types.find((t) => MediaRecorder.isTypeSupported(t));
    const rec = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    const chunks = [];
    rec.ondataavailable = (e) => e.data.size && chunks.push(e.data);
    rec.onstop = () => {
      stream.getTracks().forEach((t) => t.stop());
      this._blob = new Blob(chunks, { type: (rec.mimeType || "audio/webm").split(";")[0] });
      const preview = this.shadowRoot.getElementById("preview");
      if (preview.src) URL.revokeObjectURL(preview.src);
      preview.src = URL.createObjectURL(this._blob);
      preview.hidden = false;
      this.shadowRoot.getElementById("save").disabled = false;
      this._status("Listen back, then Save to feeder.");
    };
    this._recorder = rec;
    rec.start();
    const started = Date.now();
    const btn = this.shadowRoot.getElementById("rec");
    btn.classList.add("rec");
    const tick = () => {
      const left = MAX_SECONDS - Math.floor((Date.now() - started) / 1000);
      btn.textContent = `Stop (${Math.max(left, 0)} s)`;
      if (left <= 0) this._stop();
    };
    tick();
    this._timer = setInterval(tick, 200);
    this._status(`Recording… up to ${MAX_SECONDS} seconds.`);
  }

  _stop() {
    clearInterval(this._timer);
    const btn = this.shadowRoot.getElementById("rec");
    btn.classList.remove("rec");
    btn.textContent = "Record again";
    const rec = this._recorder;
    this._recorder = null;
    if (rec && rec.state !== "inactive") rec.stop();
  }

  async _save() {
    if (!this._blob) return;
    const save = this.shadowRoot.getElementById("save");
    save.disabled = true;
    this._status(`Saving to clip ${this._slot}…`);
    const query = this._query({ clip: String(this._slot), label: this._label.trim() });
    try {
      const res = await this._hass.fetchWithAuth(`/api/wopet_local/clip?${query}`, {
        method: "POST",
        headers: { "Content-Type": this._blob.type },
        body: this._blob,
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.message || `HTTP ${res.status}`);
      this._clips = body.clips || this._clips;
      this._renderSlots();
      this._status(`Saved ${body.seconds} s to clip ${this._slot}. Pick it in a meal's clip setting.`);
    } catch (err) {
      save.disabled = false;
      this._status(`Save failed: ${err.message || err}`, true);
    }
  }

  async _play() {
    const data = { clip: this._slot };
    if (this._config.entry_id) data.config_entry_id = this._config.entry_id;
    this._status(`Playing clip ${this._slot} on the feeder…`);
    try {
      await this._hass.callService("wopet_local", "play_clip", data);
      this._status(`Played clip ${this._slot}.`);
    } catch (err) {
      this._status(`Play failed: ${err.message || err}`, true);
    }
  }
}

if (!customElements.get("wopet-clip-card")) {
  customElements.define("wopet-clip-card", WopetClipCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "wopet-clip-card",
    name: "Wopet clip recorder",
    description: "Record a voice clip and save it to the Wopet feeder.",
  });
}

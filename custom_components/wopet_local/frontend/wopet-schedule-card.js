// Wopet feeder schedule card: the six meal slots, sorted by time, each edited in place.
// Loaded on every dashboard by the wopet_local integration; add it with
// `type: custom:wopet-schedule-card` (optional: `title`, `device_id` when you have two feeders).
//
// A change counts as saved only when Home Assistant reports the new state, not when the
// service call returns: the feeder can be offline, and a value the card shows must be a
// value the feeder has.
const DOMAIN = "wopet_local";
const CONFIRM_MS = 10000;     // the bridge answers in about a second; allow for a slow feeder
const UNDO_MS = 8000;
const STEP_IDLE_MS = 400;     // +/- taps are sent as one change once the taps stop
const FIELD = { enabled: "on/off", time: "time", name: "label", portions: "portions",
  grid: "kibble size", clip: "voice clip" };
const CHEVRON = "M7.41,8.58L12,13.17L16.59,8.58L18,10L12,16L6,10L7.41,8.58Z";

const pad = (n) => String(n).padStart(2, "0");
const hhmm = (s) => (/^\d{2}:\d{2}/.test(s || "") ? s.slice(0, 5) : undefined);
const bytes = (s) => new TextEncoder().encode(s).length;
const minutes = (t) => (t ? Number(t.slice(0, 2)) * 60 + Number(t.slice(3, 5)) : 9999);

// "630", "0630", "6:30", "18.15", "18" -> "HH:MM"; anything else -> null
export function parseTime(text) {
  const t = String(text).trim();
  let h;
  let m;
  const sep = t.match(/^(\d{1,2})\s*[:.h]\s*(\d{1,2})$/i);
  if (sep) [h, m] = [Number(sep[1]), Number(sep[2])];
  else if (/^\d{3,4}$/.test(t)) [h, m] = [Number(t.slice(0, -2)), Number(t.slice(-2))];
  else if (/^\d{1,2}$/.test(t)) [h, m] = [Number(t), 0];
  else return null;
  return h > 23 || m > 59 ? null : `${pad(h)}:${pad(m)}`;
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (v !== false && v !== undefined) node.setAttribute(k, v === true ? "" : v);
  }
  node.append(...children);
  return node;
}

function chevron() {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.classList.add("chev");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", CHEVRON);
  svg.append(path);
  return svg;
}

class WopetScheduleCard extends HTMLElement {
  constructor() {
    super();
    this._config = {};
    this._pending = new Map();   // "slot:role" -> {value, token, timer}
    this._errors = new Map();    // "slot:role" -> {value, message}
    this._steps = new Map();     // slot -> {value, timer} while +/- taps are coming in
    this._undo = new Map();      // slot -> timer
    this._hints = new Map();     // slot -> {text, bad}
    this._rows = new Map();      // slot -> DOM refs
    this._order = [];
    this._open = null;
    this.attachShadow({ mode: "open" });
  }

  static getStubConfig() {
    return { title: "Feeding schedule" };
  }

  setConfig(config) {
    this._config = { ...(config || {}) };
    this._rows.clear();
    this.shadowRoot.innerHTML = "";
    if (this._hass) this._update();
  }

  set hass(hass) {
    this._hass = hass;
    this._update();
  }

  getCardSize() {
    return 7;
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6 };
  }

  // ── data ──
  _discover() {
    const devices = new Map();
    for (const [id, ent] of Object.entries(this._hass.entities || {})) {
      if (ent.platform !== DOMAIN) continue;
      if (this._config.device_id && ent.device_id !== this._config.device_id) continue;
      const st = this._hass.states[id];
      if (!st) continue;
      // `slot` is missing while an entity is unavailable; the entity id still carries it
      const slot = Number(st.attributes.slot ?? (id.match(/_meal_(\d)(?:_|$)/) || [])[1]);
      const role = slot && this._role(id, st);
      if (!role) continue;
      const dev = ent.device_id || "";
      if (!devices.has(dev)) devices.set(dev, new Map());
      const meals = devices.get(dev);
      if (!meals.has(slot)) meals.set(slot, { slot });
      meals.get(slot)[role] = id;
    }
    const first = [...devices.keys()].sort()[0];
    return first === undefined ? new Map() : devices.get(first);
  }

  _role(id, st) {
    switch (id.split(".")[0]) {
      case "switch": return id.endsWith("_audio") ? null : "enabled";
      case "time": return "time";
      case "text": return "name";
      case "number": return "portions";
      case "select":
        return (st.attributes.options || []).includes("Silent") || id.endsWith("_clip")
          ? "clip" : "grid";
      default: return null;
    }
  }

  _st(meal, role) {
    const st = meal[role] && this._hass.states[meal[role]];
    return st && st.state !== "unavailable" && st.state !== "unknown" ? st : undefined;
  }

  _current(meal, role) {
    const st = this._st(meal, role);
    if (!st) return undefined;
    if (role === "enabled") return st.state === "on";
    if (role === "time") return hhmm(st.state);
    if (role === "portions") return Number(st.state);
    return st.state;
  }

  _shown(meal, role) {
    if (role === "portions" && this._steps.has(meal.slot)) return this._steps.get(meal.slot).value;
    const p = this._pending.get(`${meal.slot}:${role}`);
    return p ? p.value : this._current(meal, role);
  }

  // ── writes ──
  async _write(meal, role, value) {
    const id = meal[role];
    if (!id) return;
    const key = `${meal.slot}:${role}`;
    const data = { entity_id: id };
    let service = "set_value";
    if (role === "enabled") service = value ? "turn_on" : "turn_off";
    else if (role === "time") data.time = `${value}:00`;
    else if (role === "name" || role === "portions") data.value = value;
    else [service, data.option] = ["select_option", value];
    const old = this._pending.get(key);
    if (old) clearTimeout(old.timer);
    this._errors.delete(key);
    const token = {};
    const timer = setTimeout(
      () => this._fail(key, token, value, "the feeder didn't confirm it"), CONFIRM_MS);
    this._pending.set(key, { value, token, timer });
    this._update();
    try {
      await this._hass.callService(id.split(".")[0], service, data);
    } catch (err) {
      this._fail(key, token, value, (err && err.message) || String(err));
      return;
    }
    this._update();
  }

  _fail(key, token, value, message) {
    const p = this._pending.get(key);
    if (!p || p.token !== token) return;
    clearTimeout(p.timer);
    this._pending.delete(key);
    this._errors.set(key, { value, message });
    const [slot, role] = key.split(":");
    this._announce(`Meal ${slot}: couldn't save the ${FIELD[role]}.`);
    this._update();
  }

  _confirmPending(meals) {
    for (const [key, p] of this._pending) {
      const [slot, role] = key.split(":");
      const meal = meals.get(Number(slot));
      if (meal && this._current(meal, role) === p.value) {
        clearTimeout(p.timer);
        this._pending.delete(key);
        this._announce(`Meal ${slot}: ${FIELD[role]} saved.`);
      }
    }
  }

  _announce(text) {
    const live = this.shadowRoot.getElementById("live");
    if (live) live.textContent = text;
  }

  // ── render ──
  _update() {
    if (!this._hass) return;
    const meals = this._discover();
    this._meals = meals;
    this._confirmPending(meals);
    if (!this.shadowRoot.getElementById("card")) this._buildShell();
    const list = this.shadowRoot.getElementById("list");
    const empty = this.shadowRoot.getElementById("empty");
    empty.hidden = meals.size > 0;
    if (!meals.size) {
      list.replaceChildren();
      this._rows.clear();
      return;
    }
    for (const meal of meals.values()) {
      if (!this._rows.has(meal.slot)) list.append(this._buildRow(meal));
    }
    const busy = this._open !== null || this._pending.size || this._errors.size || this._steps.size;
    if (!busy || this._order.length !== meals.size) {
      // Re-sort only when nothing is open or in flight, so a row never moves under a thumb.
      this._order = [...meals.values()]
        .sort((a, b) => minutes(this._current(a, "time")) - minutes(this._current(b, "time"))
          || a.slot - b.slot)
        .map((m) => m.slot);
      for (const slot of this._order) list.append(this._rows.get(slot).root);
    }
    for (const meal of meals.values()) this._renderRow(meal);
  }

  _buildShell() {
    const card = el("ha-card", { id: "card" });
    if (this._config.title) card.append(el("h1", { class: "title", text: this._config.title }));
    card.append(
      el("div", { id: "list", class: "list" }),
      el("p", { id: "empty", class: "empty", hidden: true,
        text: "No Wopet feeder schedule found. Set up the Wopet Local integration with the bridge's control API first." }),
      el("div", { id: "live", class: "sr", "aria-live": "polite" }),
    );
    const style = el("style");
    style.textContent = STYLE;
    this.shadowRoot.replaceChildren(style, card);
  }

  _buildRow(meal) {
    const s = meal.slot;
    const r = { root: el("div", { class: "meal" }) };
    r.summary = el("button", { class: "summary", "aria-expanded": "false",
      "aria-controls": `panel-${s}` });
    r.time = el("span", { class: "time" });
    r.name = el("span", { class: "name" });
    r.tag = el("span", { class: "tag" });
    r.summary.append(r.time, r.name, r.tag, chevron());
    r.summary.addEventListener("click", () => this._toggleOpen(s));
    r.undo = el("button", { class: "text-btn", hidden: true, text: "Undo" });
    r.undo.addEventListener("click", () => {
      clearTimeout(this._undo.get(s));
      this._undo.delete(s);
      this._write(this._meals.get(s), "enabled", true);
    });
    r.sw = el("button", { class: "switch", role: "switch", "aria-label": `Meal ${s} on` },
      el("span", { class: "track" }, el("span", { class: "knob" })));
    r.sw.addEventListener("click", () => this._flip(s));
    r.root.append(el("div", { class: "head" }, r.summary, r.undo, r.sw));

    r.panel = el("div", { class: "panel", id: `panel-${s}`, hidden: true });
    // time + label
    r.timeIn = el("input", { class: "pill", id: `time-${s}`, inputmode: "numeric",
      autocomplete: "off", maxlength: "5", enterkeyhint: "done", "aria-describedby": `hint-${s}` });
    r.nameIn = el("input", { class: "pill", id: `name-${s}`, maxlength: "31", autocomplete: "off",
      enterkeyhint: "done", "aria-describedby": `hint-${s}` });
    this._editable(meal, r.timeIn, "time");
    this._editable(meal, r.nameIn, "name");
    r.timeField = this._field("Time", `time-${s}`, r.timeIn, "c-primary narrow");
    r.nameField = this._field("Label", `name-${s}`, r.nameIn, "c-primary");
    r.hint = el("p", { class: "hint", id: `hint-${s}`, hidden: true });
    // portions
    r.minus = el("button", { "aria-label": `Fewer portions, meal ${s}`, text: "−" });
    r.plus = el("button", { "aria-label": `More portions, meal ${s}`, text: "+" });
    r.count = el("output", { "aria-live": "polite" });
    r.minus.addEventListener("click", () => this._step(s, -1));
    r.plus.addEventListener("click", () => this._step(s, 1));
    r.stepper = el("div", { class: "pill stepper", role: "group", "aria-labelledby": `cap-portions-${s}` },
      r.minus, r.count, r.plus);
    r.portionsField = this._field("Portions", null, r.stepper, "c-amber", `cap-portions-${s}`);
    // kibble size
    r.seg = el("div", { class: "pill seg", role: "radiogroup", "aria-labelledby": `cap-grid-${s}` });
    r.segButtons = [];
    r.gridField = this._field("Kibble size", null, r.seg, "c-primary", `cap-grid-${s}`);
    r.seg.addEventListener("keydown", (e) => {
      if (!["ArrowLeft", "ArrowRight"].includes(e.key)) return;
      const i = r.segButtons.indexOf(this.shadowRoot.activeElement);
      const next = r.segButtons[(i + (e.key === "ArrowRight" ? 1 : -1) + r.segButtons.length) % r.segButtons.length];
      if (next) { next.focus(); next.click(); }
    });
    // voice clip
    r.select = el("select", { class: "pill", id: `clip-${s}` });
    r.select.addEventListener("change", () => this._write(this._meals.get(s), "clip", r.select.value));
    r.clipField = this._field("Voice clip", `clip-${s}`,
      el("div", { class: "select-wrap" }, r.select, chevron()), "c-purple");
    r.errors = el("div", { class: "errors" });
    r.panel.append(
      el("div", { class: "pair pair-time" }, r.timeField, r.nameField), r.hint,
      el("div", { class: "pair" }, r.portionsField, r.gridField),
      r.clipField, r.errors);
    r.root.append(r.panel);
    this._rows.set(s, r);
    return r.root;
  }

  _field(caption, forId, control, cls, capId) {
    const cap = forId
      ? el("label", { class: "cap", for: forId, text: caption })
      : el("span", { class: "cap", id: capId, text: caption });
    const status = el("span", { class: "status", text: "Saving…" });
    return el("div", { class: `field ${cls}` }, el("div", { class: "cap-row" }, cap, status), control);
  }

  _editable(meal, input, role) {
    input.addEventListener("input", () => { input.dataset.dirty = "1"; });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") input.blur();
      if (e.key === "Escape") {
        delete input.dataset.dirty;
        this._hints.delete(meal.slot);
        input.blur();
        this._update();
      }
    });
    input.addEventListener("blur", () => this._commit(meal.slot, input, role));
  }

  _commit(slot, input, role) {
    if (!input.dataset.dirty) return;
    delete input.dataset.dirty;
    const meal = this._meals.get(slot);
    const current = this._current(meal, role);
    let value;
    if (role === "time") {
      value = parseTime(input.value);
      if (!value) {
        this._hints.set(slot, { bad: true,
          text: `“${input.value}” isn't a time. Type 24-hour time, like 0630 or 18:15.` });
        this._update();
        return;
      }
    } else {
      value = input.value.trim();
      if (!value || bytes(value) > 31) {
        this._hints.set(slot, { bad: true, text: value
          ? "That label is too long for the feeder (31 bytes; emoji and accents count double)."
          : "A meal needs a label." });
        this._update();
        return;
      }
    }
    this._hints.delete(slot);
    if (value === current) this._update();
    else this._write(meal, role, value);
  }

  _flip(slot) {
    const meal = this._meals.get(slot);
    const next = !this._shown(meal, "enabled");
    clearTimeout(this._undo.get(slot));
    this._undo.delete(slot);
    if (!next) {
      this._undo.set(slot, setTimeout(() => { this._undo.delete(slot); this._update(); }, UNDO_MS));
    }
    this._write(meal, "enabled", next);
  }

  _step(slot, delta) {
    const meal = this._meals.get(slot);
    const st = this._st(meal, "portions");
    const lo = Number(st?.attributes.min ?? 1);
    const hi = Number(st?.attributes.max ?? 10);
    const prev = this._steps.get(slot);
    clearTimeout(prev?.timer);
    const value = Math.min(hi, Math.max(lo, (prev ? prev.value : this._shown(meal, "portions")) + delta));
    const timer = setTimeout(() => {
      this._steps.delete(slot);
      if (value !== this._current(meal, "portions")) this._write(meal, "portions", value);
      else this._update();
    }, STEP_IDLE_MS);
    this._steps.set(slot, { value, timer });
    this._update();
  }

  _toggleOpen(slot) {
    const prev = this._open !== null && this._rows.get(this._open);
    if (prev) {
      // leaving a meal saves what was typed there, as a blur would
      this._commit(this._open, prev.timeIn, "time");
      this._commit(this._open, prev.nameIn, "name");
    }
    this._open = this._open === slot ? null : slot;
    this._update();
    if (this._open !== null) this._rows.get(slot).summary.scrollIntoView({ block: "nearest" });
  }

  _renderRow(meal) {
    const s = meal.slot;
    const r = this._rows.get(s);
    const online = ["enabled", "time", "name"].some((role) => this._st(meal, role));
    const enabled = this._shown(meal, "enabled");
    const time = this._shown(meal, "time");
    const name = this._shown(meal, "name");
    const keys = ["enabled", "time", "name", "portions", "grid", "clip"];
    const busy = keys.some((k) => this._pending.has(`${s}:${k}`)) || this._steps.has(s);
    const errs = keys.filter((k) => this._errors.has(`${s}:${k}`));
    const open = this._open === s;

    r.root.classList.toggle("off", online && !enabled);
    r.root.classList.toggle("offline", !online);
    r.time.textContent = online ? time || "--:--" : "—";
    r.name.textContent = online ? name || `Meal ${s}` : `Meal ${s}`;
    r.tag.className = "tag";
    if (!online) [r.tag.textContent, r.tag.className] = ["Unavailable", "tag muted"];
    else if (errs.length) [r.tag.textContent, r.tag.className] = ["Not saved", "tag err"];
    else if (busy) [r.tag.textContent, r.tag.className] = ["Saving…", "tag muted"];
    else if (!enabled) [r.tag.textContent, r.tag.className] = ["Off", "tag muted"];
    else r.tag.textContent = "";
    r.summary.setAttribute("aria-expanded", String(open));
    r.summary.setAttribute("aria-label",
      `Meal ${s}, ${online ? `${time}, ${name}, ${enabled ? "on" : "off"}` : "unavailable"}`);
    r.undo.hidden = !this._undo.has(s) || enabled !== false;
    r.sw.setAttribute("aria-checked", String(!!enabled));
    r.sw.disabled = !online || this._pending.has(`${s}:enabled`);
    r.panel.hidden = !open;
    if (!open) return;

    const lock = (role) => !online || !this._st(meal, role) || this._pending.has(`${s}:${role}`);
    const setInput = (input, role, value) => {
      const focused = this.shadowRoot.activeElement === input;
      if (!focused && !input.dataset.dirty) input.value = value ?? "";
      input.disabled = lock(role);
    };
    setInput(r.timeIn, "time", time);
    setInput(r.nameIn, "name", name);
    r.timeIn.placeholder = "06:30";
    const hint = this._hints.get(s);
    r.hint.hidden = !hint;
    r.hint.textContent = hint ? hint.text : "";
    r.hint.classList.toggle("bad", !!hint?.bad);
    r.timeIn.setAttribute("aria-invalid", String(!!hint?.bad && /time/.test(hint.text)));

    const portions = this._shown(meal, "portions");
    const pst = this._st(meal, "portions");
    r.count.textContent = portions ?? "—";
    r.minus.disabled = lock("portions") || portions <= Number(pst?.attributes.min ?? 1);
    r.plus.disabled = lock("portions") || portions >= Number(pst?.attributes.max ?? 10);

    const gst = this._st(meal, "grid");
    const opts = gst?.attributes.options || ["small", "big"];
    if (r.segButtons.length !== opts.length) {
      r.segButtons = opts.map((opt) => {
        const b = el("button", { role: "radio", text: opt[0].toUpperCase() + opt.slice(1) });
        b.addEventListener("click", () => {
          if (this._shown(this._meals.get(s), "grid") !== opt) this._write(this._meals.get(s), "grid", opt);
        });
        return b;
      });
      r.seg.replaceChildren(...r.segButtons);
    }
    const grid = this._shown(meal, "grid");
    r.segButtons.forEach((b, i) => {
      const on = opts[i] === grid;
      b.setAttribute("aria-checked", String(on));
      b.tabIndex = on || (grid === undefined && i === 0) ? 0 : -1;
      b.disabled = lock("grid");
    });

    const cst = this._st(meal, "clip");
    const clips = cst?.attributes.options || [];
    const clip = this._shown(meal, "clip");
    if (r.select.dataset.opts !== clips.join("\n")) {
      r.select.replaceChildren(...clips.map((opt) =>
        el("option", { value: opt, text: /^Clip \d$/.test(opt) ? `${opt} (empty)` : opt })));
      r.select.dataset.opts = clips.join("\n");
    }
    if (this.shadowRoot.activeElement !== r.select) r.select.value = clip ?? "";
    r.select.disabled = lock("clip");

    for (const [field, role] of [[r.timeField, "time"], [r.nameField, "name"],
      [r.portionsField, "portions"], [r.gridField, "grid"], [r.clipField, "clip"]]) {
      field.classList.toggle("busy", this._pending.has(`${s}:${role}`)
        || (role === "portions" && this._steps.has(s)));
    }

    r.errors.replaceChildren(...errs.map((role) => {
      const key = `${s}:${role}`;
      const err = this._errors.get(key);
      const retry = el("button", { class: "text-btn", text: "Retry" });
      const dismiss = el("button", { class: "text-btn", text: "Dismiss" });
      retry.addEventListener("click", () => this._write(this._meals.get(s), role, err.value));
      dismiss.addEventListener("click", () => { this._errors.delete(key); this._update(); });
      const now = this._current(meal, role);
      const shown = role === "enabled" ? (now ? "on" : "off") : now;
      return el("div", { class: "error", role: "alert" },
        el("span", { class: "msg", text: `Couldn't save the ${FIELD[role]}: ${err.message}.`
          + (shown !== undefined ? ` The feeder still has ${shown}.` : "") }),
        el("span", { class: "actions" }, retry, dismiss));
    }));
  }
}

const STYLE = `
:host { --h: var(--feature-height, 42px); --r: var(--feature-border-radius, 12px); }
[hidden] { display: none !important; }
ha-card { padding: 4px 0; container-type: inline-size; overflow: hidden; }
.title { margin: 0; padding: 12px 16px 4px; font-size: var(--ha-card-header-font-size, 24px);
  font-weight: var(--ha-font-weight-normal, 400); color: var(--ha-card-header-color, var(--primary-text-color)); }
.sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
.empty { margin: 0; padding: 16px; color: var(--secondary-text-color); }
button { font: inherit; color: inherit; }
button:focus-visible, input:focus-visible, select:focus-visible {
  outline: 2px solid var(--primary-color); outline-offset: 2px; }
.meal + .meal { border-top: 1px solid var(--divider-color); }
.head { display: flex; align-items: center; gap: 12px; padding: 0 8px 0 4px; }
.summary { flex: 1; min-width: 0; min-height: 56px; display: flex; align-items: center; gap: 12px;
  padding: 0 8px 0 12px; border: 0; border-radius: var(--r); background: none; text-align: left;
  cursor: pointer; }
.summary:hover { background: rgba(var(--rgb-primary-text-color, 0, 0, 0), 0.05); }
.time { flex: none; width: 3.1em; font-weight: 500; font-variant-numeric: tabular-nums; white-space: nowrap; }
.name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.meal.off .time, .meal.off .name, .meal.offline .time, .meal.offline .name { opacity: 0.55; }
.tag:empty { display: none; }
.tag { flex: none; font-size: 12px; line-height: 20px; padding: 0 8px; border-radius: 10px; white-space: nowrap; }
.tag.muted { color: var(--secondary-text-color); border: 1px solid var(--divider-color); }
.tag.err { color: var(--error-color); background: color-mix(in srgb, var(--error-color) 14%, transparent); }
.chev { flex: none; width: 24px; height: 24px; fill: var(--secondary-text-color); transition: transform 0.2s; }
[aria-expanded="true"] > .chev { transform: rotate(180deg); }
.switch { flex: none; width: 52px; height: 44px; display: grid; place-items: center; border: 0;
  background: none; padding: 0; cursor: pointer; }
.track { position: relative; width: 36px; height: 20px; border-radius: 10px;
  background: var(--switch-unchecked-track-color, rgba(120, 120, 120, 0.45)); transition: background 0.2s; }
.knob { position: absolute; top: 2px; left: 2px; width: 16px; height: 16px; border-radius: 50%;
  background: var(--switch-unchecked-button-color, #fff); box-shadow: 0 1px 2px rgba(0, 0, 0, 0.3);
  transition: transform 0.2s; }
.switch[aria-checked="true"] .track { background: var(--switch-checked-track-color, var(--green-color, #4caf50)); }
.switch[aria-checked="true"] .knob { transform: translateX(16px); background: var(--switch-checked-button-color, #fff); }
.switch:disabled, .text-btn:disabled { opacity: 0.5; cursor: default; }
.text-btn { flex: none; min-height: 44px; padding: 0 10px; border: 0; background: none; cursor: pointer;
  color: var(--primary-color); font-weight: 500; }
.panel { display: grid; gap: 12px; padding: 4px 16px 16px; }
.pair { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 8px; }
.pair-time { grid-template-columns: 6.5em minmax(0, 1fr); }
@container (max-width: 300px) { .pair, .pair-time { grid-template-columns: minmax(0, 1fr); } }
.field { display: grid; gap: 4px; min-width: 0; }
.c-primary { --c: var(--primary-color); }
.c-amber { --c: var(--amber-color, #ffc107); }
.c-purple { --c: var(--purple-color, #926bc7); }
.cap-row { display: flex; justify-content: space-between; gap: 8px; padding: 0 4px; }
.cap { font-size: 12px; color: var(--secondary-text-color); }
.status { display: none; font-size: 12px; color: var(--secondary-text-color); }
.field.busy .status { display: inline; }
.field.busy .pill { opacity: 0.7; }
.pill { box-sizing: border-box; width: 100%; height: var(--h); border: 0; border-radius: var(--r);
  background: color-mix(in srgb, var(--c) 20%, transparent); color: var(--primary-text-color);
  font: inherit; font-size: 16px; }
input.pill { padding: 0 12px; }
input.pill.narrow, .narrow input.pill { text-align: center; font-variant-numeric: tabular-nums; }
.narrow input.pill { font-weight: 500; }
input.pill[aria-invalid="true"] { box-shadow: inset 0 0 0 2px var(--error-color); }
.pill:disabled, .pill button:disabled { cursor: default; opacity: 0.5; }
.stepper { display: flex; align-items: center; justify-content: space-between; }
.stepper button { width: 44px; height: 100%; border: 0; background: none; font-size: 22px; cursor: pointer;
  border-radius: var(--r); }
.stepper output { font-weight: 500; font-variant-numeric: tabular-nums; }
.seg { display: flex; gap: 4px; padding: 4px; }
.seg button { flex: 1; border: 0; border-radius: calc(var(--r) - 4px); background: none; cursor: pointer; }
.seg button[aria-checked="true"] { background: var(--c); color: var(--text-primary-color, #fff); font-weight: 500; }
.select-wrap { position: relative; }
.select-wrap select { appearance: none; -webkit-appearance: none; padding: 0 40px 0 12px; cursor: pointer;
  text-overflow: ellipsis; }
.select-wrap .chev { position: absolute; right: 10px; top: 9px; pointer-events: none; }
.hint { margin: -4px 4px 0; font-size: 12px; color: var(--secondary-text-color); }
.hint.bad { color: var(--error-color); }
.errors:empty { display: none; }
.errors { display: grid; gap: 8px; }
.error { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 4px 8px;
  padding: 6px 4px 6px 12px; border-radius: var(--r); color: var(--error-color);
  background: color-mix(in srgb, var(--error-color) 14%, transparent); }
.error .msg { flex: 1 1 12em; }
.error .actions { display: flex; }
@media (prefers-reduced-motion: reduce) { .chev, .track, .knob { transition: none; } }
`;

if (!customElements.get("wopet-schedule-card")) {
  customElements.define("wopet-schedule-card", WopetScheduleCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "wopet-schedule-card",
    name: "Wopet feeding schedule",
    description: "The feeder's six meals, sorted by time, each edited in place.",
  });
}

// Pantry Hub web UI: routing, live updates, actions, door check flow.
import { readBoot, Api, Poller } from "./api.js?v=__VERSION__";
import * as V from "./views.js?v=__VERSION__";
import { openScanner } from "./scanner.js?v=__VERSION__";

const boot = readBoot();
const api = new Api(boot);
const root = document.getElementById("app");
const view = document.getElementById("view");
const titleEl = document.getElementById("title");
const freshEl = document.getElementById("fresh");

const TITLES = { pantry: "Pantry", tags: "Tags", settings: "Settings" };
const CHECK_HOLD_MS = 3000; // the door must stay open this long
const CHECK_TIMEOUT_MS = 60000;

const ui = {
  api,
  tab: "pantry",
  detailsOpen: false,
  noPicture: new Set(), // "productId:version" without a picture
  skipOpen: new Set(),
  olderOpen: false,
  fullEpc: new Set(),
  howTo: new Set(),
  menu: null,
  search: "",
  pickerSearch: "",
  cands: null,
  candError: null,
  door: null, // live /api/door on Settings
  check: { phase: "idle" },
};
let state = null;
let lastOk = null;
let failures = 0;
const slotCache = new Map();
const pendingSlots = new Set();
let doorTimer = null;

// Layout per tab: static parts (search fields) stay put; slots re-render. ---

const LAYOUTS = {
  pantry: () => `
<div id="s-health"></div>
<div id="s-door"></div>
<div id="s-attention" class="stack"></div>
<section id="s-bought" class="stack" aria-label="Just bought"></section>
<section id="s-items" class="stack" aria-label="In the pantry"></section>`,
  tags: () => `
<section id="s-stocktake" class="stack" aria-label="Stock-take"></section>
<section id="s-unlinked" class="stack" aria-label="Not linked yet"></section>
<section class="stack" aria-labelledby="linked-h">
<div class="section-head"><h2 id="linked-h">Linked</h2></div>
<label class="field"><span class="visually-hidden">Search linked tags</span>
<input id="tag-search" class="input" type="search" placeholder="Search linked tags" autocomplete="off" enterkeyhint="search"></label>
<div id="s-linked"></div>
</section>
<section id="s-grocy-untagged" class="stack" aria-label="In Grocy, not tagged"></section>
<section id="s-allowances" class="stack" aria-label="Waiting for tags"></section>
<section id="s-failed" class="stack" aria-label="Failed Grocy updates"></section>
<section id="s-needs" class="stack" aria-label="Barcodes without a Grocy product"></section>`,
  settings: () => `
<div class="section-head"><h2>Pantry door sensor</h2></div>
<div class="card stack">
<div id="s-source"></div>
<label class="field"><span>Sensor</span>
<input id="picker-search" class="input" type="search" placeholder="Search by name or entity id" autocomplete="off"></label>
<div id="s-picker"></div>
<div id="s-live"></div>
<div id="s-check"></div>
<details>
<summary>Advanced</summary>
<label class="switch"><span>Reverse open/closed</span><input id="inverted" type="checkbox" role="switch"></label>
<p class="sub">Only for a sensor that reports open as closed and can’t be changed. Fix it at the device first if you can: wire the reed switch the other way (NC/NO), or use the sensor’s own invert setting.</p>
</details>
<details class="help">
<summary>Help</summary>
<p>Pantry Hub reads the pantry continuously while the door is open and checks once more when it closes. While it’s closed it checks every hour, so items taken out with the door shut are noticed too.</p>
<p>The list shows every binary sensor in Home Assistant, door, garage door, opening and window sensors first. Your choice is saved in Pantry Hub. If <code>door_entity</code> is set in the app’s Configuration tab, that one is used instead.</p>
<p>Without a door sensor Pantry Hub still works, but only checks once an hour.</p>
</details>
</div>
<div class="section-head"><h2>Reader</h2></div>
<div class="card" id="s-reader"></div>
<p class="sub">Change these in the app’s Configuration tab.</p>
<div class="section-head"><h2>Grocy</h2></div>
<div class="card stack" id="s-resync"></div>
<div class="section-head"><h2>Product pictures</h2></div>
<div class="card stack" id="s-pictures"></div>`,
};

function slotsFor(tab) {
  if (!state) return {};
  if (tab === "pantry") {
    return {
      "s-health": () => V.healthBanner(state, ui),
      "s-door": () => V.doorCard(state),
      "s-attention": () => V.attention(state),
      "s-bought": () => V.justBought(state, ui),
      "s-items": () => V.pantryItems(state, ui),
    };
  }
  if (tab === "tags") {
    return {
      "s-stocktake": () => V.stocktakeCard(state),
      "s-unlinked": () => V.unlinkedCards(state, ui),
      "s-grocy-untagged": () => V.grocyUntagged(state, ui),
      "s-allowances": () => V.allowanceList(state, ui),
      "s-linked": () => V.linkedList(state, ui),
      "s-failed": () => V.failedOps(state),
      "s-needs": () => V.needsProduct(state),
    };
  }
  const door = ui.door || doorFromState();
  return {
    "s-source": () => V.doorSource(state),
    "s-picker": () => V.pickerList(ui.cands, ui, pickerCurrent()),
    "s-live": () => V.liveDoor(door),
    "s-check": () => V.checkStep(ui, door),
    "s-reader": () => V.readerOptions(state),
    "s-resync": () => V.resyncCard(state),
    "s-pictures": () => V.picturesCard(state),
  };
}

function doorFromState() {
  if (!state) return null;
  const d = state.door;
  return { entity_id: d.entity_id, door: d.state, raw: d.raw, inverted: d.inverted, missing: d.missing };
}

function pickerCurrent() {
  if (!state) return null;
  return state.door.source === "web_ui" ? state.door.entity_id : state.door.web_entity_id;
}

// Rendering --------------------------------------------------------------------

function setTab(tab) {
  if (!TITLES[tab]) tab = "pantry";
  const changed = tab !== ui.tab || !view.dataset.tab;
  ui.tab = tab;
  titleEl.textContent = TITLES[tab];
  document.title = tab === "pantry" ? "Pantry Hub" : `${TITLES[tab]} · Pantry Hub`;
  for (const a of root.querySelectorAll(".tabs a")) {
    if (a.dataset.tab === tab) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
  if (changed) {
    view.dataset.tab = tab;
    view.innerHTML = state ? LAYOUTS[tab]() + footer() : skeleton();
    slotCache.clear();
    pendingSlots.clear();
    wireStatic();
  }
  if (tab === "settings") startDoorPolling();
  else stopDoorPolling();
  render();
}

const skeleton = () => `<div class="skeleton" aria-hidden="true"><div></div><div></div><div></div></div>`;
const footer = () => `<p class="foot">Pantry Hub ${V.esc(boot.version)}</p>`;

function render() {
  if (!state) return;
  if (!view.querySelector("[id^='s-']")) {
    view.innerHTML = LAYOUTS[ui.tab]() + footer();
    slotCache.clear();
    wireStatic();
  }
  for (const [id, make] of Object.entries(slotsFor(ui.tab))) renderSlot(id, make);
  if (ui.tab === "tags") {
    const h = document.getElementById("linked-h");
    if (h) h.textContent = `Linked · ${state.links.length}`;
  }
  if (ui.tab === "settings") {
    const sw = document.getElementById("inverted");
    const inv = ui.door ? ui.door.inverted : state.door.inverted;
    if (sw && document.activeElement !== sw) sw.checked = !!inv;
    if (sw) sw.disabled = !state.door.entity_id;
  }
}

/** Replace a slot only if its HTML changed, never under the user's cursor. */
function renderSlot(id, make) {
  const el = document.getElementById(id);
  if (!el) return;
  const html = make();
  if (slotCache.get(id) === html) return;
  const active = document.activeElement;
  if (active && el.contains(active) && /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName)) {
    pendingSlots.add(id); // re-render after the field loses focus
    return;
  }
  const kept = keepFields(el);
  el.innerHTML = html;
  restoreFields(el, kept);
  slotCache.set(id, html);
  pendingSlots.delete(id);
}

function keepFields(el) {
  const kept = [];
  for (const input of el.querySelectorAll("form[data-form] input")) {
    const form = input.closest("form");
    const key = `${form.dataset.form}|${form.dataset.epc}|${input.name}`;
    kept.push([key, input.type === "checkbox" ? input.checked : input.value]);
  }
  return kept;
}

function restoreFields(el, kept) {
  for (const [key, value] of kept) {
    const [form, epc, name] = key.split("|");
    const input = el.querySelector(`form[data-form="${CSS.escape(form)}"][data-epc="${CSS.escape(epc)}"] input[name="${CSS.escape(name)}"]`);
    if (!input) continue;
    if (input.type === "checkbox") input.checked = value;
    else input.value = value;
  }
}

function wireStatic() {
  const search = document.getElementById("tag-search");
  if (search) {
    search.value = ui.search;
    search.addEventListener("input", () => {
      ui.search = search.value;
      renderSlot("s-linked", () => V.linkedList(state, ui));
    });
  }
  const picker = document.getElementById("picker-search");
  if (picker) {
    picker.value = ui.pickerSearch;
    picker.addEventListener("input", () => {
      ui.pickerSearch = picker.value;
      renderSlot("s-picker", () => V.pickerList(ui.cands, ui, pickerCurrent()));
    });
    loadCandidates();
  }
  const sw = document.getElementById("inverted");
  if (sw) {
    sw.addEventListener("change", async () => {
      const res = await api.post("/settings/inverted", { inverted: sw.checked });
      toast(res);
      refreshDoor();
      poller.now();
    });
  }
}

// Freshness and errors -------------------------------------------------------

function updateFresh() {
  let banner = document.getElementById("offline");
  if (failures >= 3) {
    if (!banner) {
      banner = document.createElement("div");
      banner.id = "offline";
      banner.className = "offline";
      banner.setAttribute("role", "alert");
      banner.textContent = "Can’t reach Pantry Hub — retrying";
      root.prepend(banner);
    }
  } else if (banner) {
    banner.remove();
  }
  if (lastOk == null) {
    freshEl.textContent = failures ? "Can’t load yet — retrying" : "Loading…";
    return;
  }
  const s = Math.max(0, Math.round((Date.now() - lastOk) / 1000));
  freshEl.textContent = s < 2 ? "Updated just now" : `Updated ${s} s ago`;
}

let toastTimer = null;
function toast(res) {
  let el = document.getElementById("toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "toast";
    el.setAttribute("role", "status");
    el.setAttribute("aria-live", "polite");
    document.body.append(el);
  }
  el.className = res.ok ? "toast" : "toast bad";
  el.textContent = res.message || (res.ok ? "Done" : "Something went wrong");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.hidden = true;
  }, 4000);
}

// Live data --------------------------------------------------------------------

const poller = new Poller(() => api.get("/api/state"), {
  onData(data) {
    state = data;
    lastOk = Date.now();
    failures = 0;
    root.removeAttribute("aria-busy");
    render();
    updateFresh();
  },
  onError(n) {
    failures = n;
    updateFresh();
  },
});

async function loadCandidates() {
  try {
    const data = await api.get("/api/door-candidates");
    ui.cands = data.candidates;
    ui.candError = data.error;
  } catch (err) {
    ui.cands = [];
    ui.candError = String(err.message || err);
  }
  renderSlot("s-picker", () => V.pickerList(ui.cands, ui, pickerCurrent()));
}

async function refreshDoor() {
  try {
    ui.door = await api.get("/api/door");
  } catch {
    return;
  }
  advanceCheck();
  if (ui.tab === "settings") render();
}

function startDoorPolling() {
  if (doorTimer) return;
  refreshDoor();
  doorTimer = setInterval(() => {
    if (!document.hidden) refreshDoor();
  }, 1000);
}

function stopDoorPolling() {
  clearInterval(doorTimer);
  doorTimer = null;
}

// Door check: the person opens the door; the raw state must say open. -----------

function advanceCheck() {
  const c = ui.check;
  const d = ui.door;
  if (c.phase !== "waiting" || !d) return;
  const now = Date.now();
  if (now - c.startedAt > CHECK_TIMEOUT_MS) {
    ui.check = { phase: "timeout" };
    return;
  }
  const raw = d.raw;
  if ((raw === "on" || raw === "off") && raw !== c.startRaw) {
    if (c.changedRaw !== raw) {
      c.changedRaw = raw;
      c.changedAt = now;
    } else if (now - c.changedAt >= CHECK_HOLD_MS) {
      ui.check = { phase: d.door === "open" ? "right" : "reversed" };
    }
  } else {
    c.changedRaw = null;
    c.changedAt = null;
  }
}

function startCheck() {
  const d = ui.door || doorFromState();
  if (!d || (d.raw !== "on" && d.raw !== "off")) {
    ui.check = { phase: "timeout" };
  } else {
    ui.check = { phase: "waiting", startRaw: d.raw, startedAt: Date.now(), changedRaw: null, changedAt: null };
  }
  render();
}

// Actions ----------------------------------------------------------------------

root.addEventListener("click", async (event) => {
  const el = event.target.closest("[data-action]");
  if (!el || el.disabled) return;
  const action = el.dataset.action;
  if (action === "go") {
    location.hash = `#/${el.dataset.tab}`;
    if (el.dataset.tab === "tags") {
      requestAnimationFrame(() => document.getElementById("s-failed")?.scrollIntoView({ block: "start" }));
    }
  } else if (action === "toggle-details") {
    ui.detailsOpen = !ui.detailsOpen;
    render();
  } else if (action === "toggle-epc") {
    toggle(ui.fullEpc, el.dataset.epc);
  } else if (action === "toggle-howto") {
    toggle(ui.howTo, el.dataset.epc);
  } else if (action === "toggle-menu") {
    ui.menu = ui.menu === el.dataset.epc ? null : el.dataset.epc;
    render();
  } else if (action === "confirm-unlink") {
    if (window.confirm(`Unlink ${el.dataset.name}? The tag will show up as unlinked next time it's read.`)) {
      ui.menu = null;
      await act("/unlink", { epc: el.dataset.epc }, el);
    }
  } else if (action === "post") {
    const fields = {};
    if (el.dataset.confirm && !window.confirm(el.dataset.confirm)) return;
    for (const key of ["epc", "op", "barcode", "row", "product_id", "mode"]) if (el.dataset[key]) fields[key] = el.dataset[key];
    await act(el.dataset.path, fields, el);
  } else if (action === "scan-start") {
    await act("/scan/start", { mode: el.dataset.mode }, el);
  } else if (action === "scan-stop") {
    await act("/scan/stop", {}, el);
  } else if (action === "scan-open") {
    openCamera(el.dataset.mode);
  } else if (action === "skip-toggle") {
    toggle(ui.skipOpen, el.dataset.row);
  } else if (action === "older-toggle") {
    event.preventDefault();
    ui.olderOpen = !ui.olderOpen;
    render();
  } else if (action === "pick-door") {
    await act("/settings/door", { door_entity: el.dataset.entity }, el);
    ui.check = { phase: "idle" };
    refreshDoor();
  } else if (action === "check-start") {
    startCheck();
  } else if (action === "check-cancel") {
    ui.check = { phase: "idle" };
    render();
  } else if (action === "check-flip") {
    const inverted = !(ui.door ? ui.door.inverted : state.door.inverted);
    const res = await api.post("/settings/inverted", { inverted });
    if (res.ok) ui.check = { phase: "flipped" };
    else toast(res);
    await refreshDoor();
    poller.now();
  }
});

function toggle(set, key) {
  if (set.has(key)) set.delete(key);
  else set.add(key);
  render();
}

async function act(path, fields, el) {
  if (el) el.disabled = true;
  try {
    const res = await api.post(path, fields);
    toast(res);
  } catch (err) {
    toast({ ok: false, message: `Failed: ${err.message || err}` });
  } finally {
    if (el) el.disabled = false;
    await poller.now();
  }
}

function openCamera(mode) {
  const title = mode === "stocktake" ? "Stock-take: scan each item" : "Unload: scan each item";
  openScanner({
    title,
    zxingUrl: api.url(`/static/zxing.min.js?v=${boot.version}`),
    async onCode(code) {
      const res = await api.post("/scan", { barcode: code, mode });
      poller.now();
      return res;
    },
    onClose: () => poller.now(),
  });
}

root.addEventListener("submit", async (event) => {
  const count = event.target.closest("form[data-form='count']");
  if (count) {
    event.preventDefault();
    await act("/stocktake/count", { product_id: count.dataset.epc, counted: count.elements.counted.value }, count.querySelector("button"));
    return;
  }
  const apply = event.target.closest("form[data-form='apply']");
  if (apply) {
    event.preventDefault();
    const how = apply.querySelector("input[name='how']:checked").value;
    const gone = apply.elements.gone.checked;
    const msg = how === "set"
      ? `Set Grocy to the counted amounts${gone ? " and mark unscanned products as gone" : ""}?`
      : `Add the missing items to Grocy as purchases${gone ? " and mark unscanned products as gone" : ""}?`;
    if (!window.confirm(msg)) return;
    await act("/stocktake/apply", { how, gone: gone ? "on" : "" }, apply.querySelector("button[type=submit]"));
    return;
  }
  const skip = event.target.closest("form[data-form='skip']");
  if (skip) {
    event.preventDefault();
    const row = skip.dataset.epc;
    const add = skip.elements.add_stock.checked ? "on" : "";
    await act("/bought/skip", { row, add_stock: add }, skip.querySelector("button[type=submit]"));
    ui.skipOpen.delete(row);
    render();
    return;
  }
  const form = event.target.closest("form[data-form='relink']");
  if (!form) return;
  event.preventDefault();
  const fields = {
    epc: form.dataset.epc,
    target: form.elements.target.value,
    in_stock: form.elements.in_stock.checked ? "on" : "",
  };
  const button = form.querySelector("button[type=submit]");
  button.disabled = true;
  const res = await api.post("/relink", fields);
  toast(res);
  button.disabled = false;
  if (res.ok) {
    ui.menu = null;
    ui.howTo.delete(form.dataset.epc);
  }
  await poller.now();
});

root.addEventListener("focusout", () => {
  setTimeout(() => {
    if (!pendingSlots.size) return;
    for (const [id, make] of Object.entries(slotsFor(ui.tab))) if (pendingSlots.has(id)) renderSlot(id, make);
  }, 0);
});

// A product without a picture (or Grocy unreachable): neutral icon instead.
view.addEventListener(
  "error",
  (event) => {
    const img = event.target;
    if (!(img instanceof HTMLImageElement) || !img.dataset.product) return;
    ui.noPicture.add(`${img.dataset.product}:${img.dataset.v || 0}`);
    const ph = document.createElement("span");
    ph.className = "pic ph";
    ph.innerHTML = V.icons.can;
    img.replaceWith(ph);
  },
  true,
);

// Start ------------------------------------------------------------------------

root.insertAdjacentHTML("beforeend", V.tabs(ui.tab));
window.addEventListener("hashchange", () => setTab(location.hash.replace(/^#\/?/, "")));
setTab(location.hash.replace(/^#\/?/, "") || "pantry");
poller.start();
setInterval(updateFresh, 1000);

// For tests and debugging in the browser console.
window.__pantryHub = { poller, ui, get state() { return state; } };

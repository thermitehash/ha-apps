// Pure render functions: data in, HTML string out. Everything inserted is
// escaped with esc(). Interactive elements carry data-action attributes that
// app.js handles with one delegated listener.

export function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

// Icons: inline stroke SVG, decorative (the text next to them says the same).
const svg = (d, extra = "") =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" ${extra}>${d}</svg>`;
export const icons = {
  ok: svg('<path d="M5 12l4 4 10-10"/>'),
  warn: svg('<path d="M12 3l9 16H3z"/><path d="M12 10v4M12 17h.01"/>'),
  problem: svg('<path d="M2 8.5a15 15 0 0 1 20 0M5.5 12a10 10 0 0 1 13 0M3 3l18 18"/>'),
  doorClosed: svg('<rect x="5" y="3" width="14" height="18" rx="1"/><path d="M15 12h.01"/>'),
  doorOpen: svg('<path d="M4 21h16M6 21V3h8v18"/><path d="M14 3l5 2v16"/><path d="M11 12h.01"/>'),
  doorNone: svg('<rect x="5" y="3" width="14" height="18" rx="1" stroke-dasharray="3 3"/>'),
  can: svg('<rect x="6" y="3" width="12" height="18" rx="2"/><path d="M6 8h12"/>'),
  chevron: svg('<path d="M9 6l6 6-6 6"/>'),
  more: svg('<circle cx="5" cy="12" r="1.2"/><circle cx="12" cy="12" r="1.2"/><circle cx="19" cy="12" r="1.2"/>'),
  pantry: svg('<path d="M4 4h16v16H4zM4 10h16M10 10v10"/>'),
  tags: svg('<path d="M3 12V4h8l10 10-8 8z"/><circle cx="7.5" cy="7.5" r="1.5"/>'),
  settings: svg('<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1L7 17M17 7l2.1-2.1"/>'),
};

// Time ---------------------------------------------------------------------

/** "just now", "3 s ago", "4 min ago", "2 h ago", "Tue", "Nov 20". */
export function ago(ts, now) {
  if (ts == null) return "never";
  const s = Math.max(0, Math.round(now - ts));
  if (s < 5) return "just now";
  if (s < 60) return `${s} s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h} h ago`;
  return day(ts, now);
}

/** "in 56 min", "in 30 s", "now". */
export function inTime(ts, now) {
  if (ts == null) return null;
  const s = Math.round(ts - now);
  if (s <= 5) return "now";
  if (s < 60) return `in ${s} s`;
  const m = Math.round(s / 60);
  if (m < 60) return `in ${m} min`;
  const h = Math.round(m / 60);
  return `in ${h} h`;
}

/** "today", "yesterday", "Tue" (this week), else "Nov 20". */
export function day(ts, now) {
  const d = new Date(ts * 1000);
  const n = new Date(now * 1000);
  const startOf = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((startOf(n) - startOf(d)) / 86400000);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 7) return d.toLocaleDateString(undefined, { weekday: "short" });
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function shortEpc(epc) {
  const e = String(epc || "");
  return e.length > 10 ? `${e.slice(0, 4)}…${e.slice(-4)}` : e;
}

const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

// Tabs ---------------------------------------------------------------------

export function tabs(current) {
  const tab = (id, label) =>
    `<a href="#/${id}" data-tab="${id}"${current === id ? ' aria-current="page"' : ""}>${icons[id]}<span>${label}</span></a>`;
  return `<nav class="tabs" aria-label="Sections">${tab("pantry", "Pantry")}${tab("tags", "Tags")}${tab("settings", "Settings")}</nav>`;
}

// Pantry -------------------------------------------------------------------

export function healthBanner(state, ui) {
  const h = state.health;
  const open = ui.detailsOpen;
  const icon = h.level === "ok" ? icons.ok : h.level === "warn" ? icons.warn : icons.problem;
  const action =
    h.action && h.action_label
      ? `<button class="btn pill" type="button" data-action="go" data-tab="${esc(h.action)}">${esc(h.action_label)}</button>`
      : "";
  let details = "";
  if (open) {
    const issues = h.issues.length > 1
      ? `<h3>All issues</h3><ul class="issues">${h.issues.map((i) => `<li>${esc(i.title)}: ${esc(i.detail)}</li>`).join("")}</ul>`
      : "";
    const rows = state.diagnostics.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
    details = `<div class="details" id="health-details">${issues}<h3>Details</h3><dl class="kv">${rows}</dl></div>`;
  }
  return `<div class="health-wrap">
<div class="row">
<button class="health ${esc(h.level)}" type="button" data-action="toggle-details" aria-expanded="${open}" aria-controls="health-details">
<span class="icon">${icon}</span>
<span class="grow"><span class="title">${esc(h.title)}</span><span class="sub">${esc(h.detail)}</span></span>
<span class="more">${open ? "Hide" : "Details"}</span>
</button>
</div>
${action ? `<div class="actions">${action}</div>` : ""}
${details}
</div>`;
}

export function doorCard(state) {
  const d = state.door;
  const r = state.reading;
  const now = state.now;
  let cls = "closed";
  let icon = icons.doorClosed;
  let word = "Door closed";
  if (!d.entity_id) {
    cls = "none";
    icon = icons.doorNone;
    word = "No door sensor";
  } else if (d.state === "open") {
    cls = "open";
    icon = icons.doorOpen;
    word = "Door open";
  } else if (d.state !== "closed") {
    cls = "none";
    icon = icons.doorNone;
    word = "Door unknown";
  }
  let sub;
  if (r.mode === "continuous") {
    sub = "Reading continuously (reader auto mode)";
  } else if (r.activity === "sweep" && state.reader.online === false) {
    sub = "Trying to reach the reader…";
  } else if (r.activity === "sweep") {
    sub = "Checking the pantry now…";
  } else if (r.activity === "continuous") {
    sub = "Reading continuously while the door is open";
  } else {
    const last = r.last_completed_sweep_at != null ? `Last check ${ago(r.last_completed_sweep_at, now)}` : "Not checked yet";
    const next = inTime(r.next_sweep_at, now);
    sub = next ? `${last} · next ${next}` : last;
  }
  const button =
    r.mode === "door"
      ? `<button class="btn pill" type="button" data-action="post" data-path="/sweep" ${r.activity === "sweep" ? "disabled" : ""}>Check now</button>`
      : "";
  return `<div class="card door ${cls} row">
<span class="icon">${icon}</span>
<span class="grow"><span class="title">${word}</span><span class="sub">${esc(sub)}</span></span>
${button}
</div>`;
}

export function attention(state) {
  const n = state.unlinked.length;
  const b = state.needs_product.length;
  const cards = [];
  if (n) {
    cards.push(`<a class="attention" href="#/tags"><span class="title">${plural(n, "tag")} ${n === 1 ? "isn’t" : "aren’t"} linked to a product</span>${icons.chevron}</a>`);
  }
  if (b) {
    cards.push(`<a class="attention" href="#/tags"><span class="title">${plural(b, "barcode")} ${b === 1 ? "needs" : "need"} a Grocy product</span>${icons.chevron}</a>`);
  }
  return cards.join("");
}

/** 40 px product thumbnail; neutral icon when there's none. */
export function pic(productId, state, ui) {
  if (productId == null) return `<span class="pic ph">${icons.can}</span>`;
  const v = (state.pictures && state.pictures.versions[String(productId)]) || 0;
  if (ui.noPicture.has(`${productId}:${v}`)) return `<span class="pic ph">${icons.can}</span>`;
  const src = ui.api.url(`/api/picture/${productId}?v=${v}`);
  return `<img class="pic" src="${esc(src)}" alt="" loading="lazy" data-product="${productId}" data-v="${v}" width="40" height="40">`;
}

// Just bought (Kroger Restock) -----------------------------------------------------

function boughtRow(r, state, ui) {
  const size = r.size ? `<span class="sub">${esc(r.size)}</span>` : "";
  const skipOpen = ui.skipOpen.has(String(r.id));
  const skip = skipOpen
    ? `<form class="inline-form" data-form="skip" data-epc="${r.id}">
<label class="check"><input type="checkbox" name="add_stock" checked> Add to Grocy stock</label>
<p class="sub">${r.to_tag === 1 ? "The untagged item is" : `The ${r.to_tag} untagged items are`} booked as a purchase. If you tag one within a week, it isn’t counted twice.</p>
<div class="actions"><button class="btn primary" type="submit">Remove from list</button><button class="btn" type="button" data-action="skip-toggle" data-row="${r.id}">Cancel</button></div>
</form>`
    : "";
  return `<li class="item stack${r.older ? " older" : ""}"><div class="row">${pic(r.product_id, state, ui)}<span class="grow"><span class="title">${esc(r.name)}</span>${size}</span><span class="tag-count">×${r.to_tag} of ${r.quantity} to tag</span></div>
<div class="actions"><button class="btn primary" type="button" data-action="post" data-path="/bought/tag" data-row="${r.id}">Tag them</button><button class="btn" type="button" data-action="skip-toggle" data-row="${r.id}" aria-expanded="${skipOpen}">Not tagging</button></div>${skip}</li>`;
}

function scanSession(state, mode) {
  const sc = state.scan;
  return sc && sc.session && sc.session.mode === mode ? sc.session : null;
}

function lastScan(state, mode) {
  const last = state.scan && state.scan.last;
  if (!last || last.mode !== mode) return "";
  const cls = last.status === "error" || last.status === "unknown_barcode" ? "error-text" : "sub";
  return `<p class="${cls}" role="status" aria-live="polite">${esc(last.message)}</p>`;
}

function untrackedDetail(u) {
  const parts = [];
  if (u.item_count != null && u.tracked_units != null) parts.push(`${u.item_count} items in the order, ${u.tracked_units} already in Just bought`);
  if (u.pickup_window) parts.push(`picked up ${u.pickup_window}`);
  parts.push("Kroger Restock couldn't match these; scan them as you put them away");
  return esc(parts.join(" · "));
}

export function justBought(state, ui) {
  const jb = state.just_bought;
  const sc = state.scan || {};
  if (!jb || (!jb.available && !sc.available)) return "";
  const fresh = jb.rows.filter((r) => !r.older);
  const older = jb.rows.filter((r) => r.older);
  const s = jb.session;
  const session = s
    ? `<div class="session" role="status" aria-live="polite"><span class="title">Tap each ${esc(s.product)} on the station</span><span class="sub">${esc(s.text)}${s.skipped ? ` · ${plural(s.skipped, "tag")} skipped (already linked)` : ""}</span></div>`
    : "";
  const unloading = scanSession(state, "unload");
  const u = jb.untracked;
  const untracked =
    u && !unloading
      ? `<div class="untracked"><span class="title">About ${plural(u.remaining, "more item")} from your Kroger order — start unloading</span><span class="sub">${untrackedDetail(u)}</span><button class="btn primary" type="button" data-action="scan-start" data-mode="unload">Start unloading</button></div>`
      : "";
  let unload = "";
  if (sc.available) {
    unload = unloading
      ? `<div class="unloading" role="status" aria-live="polite"><span class="title">Unloading${u ? ` · about ${plural(u.remaining, "item")} left from your Kroger order` : ""}</span><span class="sub">Scan each item${unloading.station ? ` at the ${esc(unloading.station)} station (green pulse)` : ""} or with your phone. ${plural(unloading.scans, "scan")} so far; stops after 3 minutes without a scan.</span>${lastScan(state, "unload")}<div class="actions"><button class="btn primary" type="button" data-action="scan-open" data-mode="unload">Scan barcode</button><button class="btn" type="button" data-action="scan-stop">Stop unloading</button></div></div>`
      : `<div class="actions"><button class="btn" type="button" data-action="scan-start" data-mode="unload">Start unloading</button><button class="btn" type="button" data-action="scan-open" data-mode="unload">Scan barcode</button></div>`;
  }
  const head = `<div class="section-head"><h2>Just bought${jb.to_tag ? ` · ${plural(jb.to_tag, "item")} to tag` : ""}</h2></div>`;
  const list = fresh.length ? `<ul class="list" role="list">${fresh.map((r) => boughtRow(r, state, ui)).join("")}</ul>` : "";
  const old = older.length
    ? `<details class="older-group"${ui.olderOpen ? " open" : ""}><summary data-action="older-toggle">Older · ${older.length}</summary><ul class="list" role="list">${older.map((r) => boughtRow(r, state, ui)).join("")}</ul></details>`
    : "";
  return head + untracked + session + unload + list + old;
}

// Stock-take (Tags tab) --------------------------------------------------------

function fmt(n) {
  return Number.isInteger(n) ? String(n) : String(Math.round(n * 100) / 100);
}

function signed(n) {
  if (n > 0) return `+${fmt(n)}`;
  return fmt(n);
}

export function stocktakeCard(state) {
  const st = state.stocktake;
  if (!st) return "";
  const head = `<div class="section-head"><h2>Stock-take</h2></div>`;
  if (!st.phase) {
    return `${head}<div class="card stack"><p class="sub">Count what's already in the pantry: scan each unit's barcode once (or scan once and type how many). Tagged items are counted by the reader, so only count the untagged ones. At the end you review the numbers before anything changes in Grocy.</p><div class="actions"><button class="btn primary" type="button" data-action="scan-start" data-mode="stocktake">Start stock-take</button></div><p class="sub">On the station: hold the button for 6 seconds (slow amber pulse).</p></div>`;
  }
  if (st.phase === "counting") {
    const counting = scanSession(state, "stocktake");
    const rows = st.counts
      .map(
        (r) => `<li class="item"><form class="row count-form" data-form="count" data-epc="${r.product_id}"><span class="grow"><span class="title">${esc(r.product_name)}</span></span><label class="qty-field"><span class="visually-hidden">How many ${esc(r.product_name)}</span><input class="input" name="counted" type="number" min="0" max="999" inputmode="numeric" value="${r.scanned}"></label><button class="btn" type="submit">Save</button></form></li>`,
      )
      .join("");
    const list = rows ? `<ul class="list" role="list">${rows}</ul>` : `<p class="sub">Nothing counted yet.</p>`;
    const where = counting
      ? `Scanning${counting.station ? ` at the ${esc(counting.station)} station (slow amber pulse)` : ""}; stops after 3 minutes without a scan.`
      : `Scanning paused. Start again to keep counting.`;
    return `${head}<div class="card stack"><span class="title">Counting · ${plural(st.counts.length, "product")}</span><span class="sub">${where}</span>${lastScan(state, "stocktake")}${list}<div class="actions"><button class="btn primary" type="button" data-action="scan-open" data-mode="stocktake">Scan barcode</button>${counting ? "" : `<button class="btn" type="button" data-action="scan-start" data-mode="stocktake">Resume scanning</button>`}<button class="btn primary" type="button" data-action="post" data-path="/stocktake/finish">Finish stock-take</button><button class="btn" type="button" data-action="post" data-path="/stocktake/close" data-confirm="Cancel this stock-take? The counts are thrown away; Grocy isn't changed.">Cancel</button></div></div>`;
  }
  const applied = st.phase === "applied";
  const rows = st.review
    .map((r) => {
      const diff = r.difference === 0 ? "—" : signed(r.difference);
      const tags = r.tags ? ` + ${r.tags} tagged` : "";
      const counted = r.scanned ? `${r.counted}${tags}` : `not scanned${tags}`;
      const note = r.applied === "set" ? "Grocy set" : r.applied === "purchase" ? "Purchase added" : r.applied === "gone" ? "Set to gone" : "";
      const tag =
        applied && r.scanned && r.to_tag > 0
          ? `<button class="btn" type="button" data-action="post" data-path="/stocktake/tag" data-product_id="${r.product_id}">Tag ${r.to_tag}</button>`
          : "";
      return `<tr${r.scanned ? "" : ' class="unscanned"'}><th scope="row">${esc(r.name)}${note ? `<span class="sub"> · ${note}</span>` : ""}</th><td>${counted}</td><td>${fmt(r.grocy)}</td><td class="${r.difference > 0 ? "pos" : r.difference < 0 ? "neg" : ""}">${diff}</td><td>${tag}</td></tr>`;
    })
    .join("");
  const table = `<div class="table-wrap"><table class="review"><thead><tr><th scope="col">Product</th><th scope="col">Counted</th><th scope="col">Grocy</th><th scope="col">Difference</th><th scope="col"><span class="visually-hidden">Tag</span></th></tr></thead><tbody>${rows || `<tr><td colspan="5">Nothing to review.</td></tr>`}</tbody></table></div>`;
  if (applied) {
    return `${head}<div class="card stack"><span class="title">Applied${st.how === "purchase" ? " as purchases" : ""}</span><span class="sub">Tag the counted items now if you like: tap Tag, then tap each item on the station. They won't be added to Grocy again when the reader sees them.</span>${table}<div class="actions"><button class="btn" type="button" data-action="post" data-path="/stocktake/close">Close stock-take</button></div></div>`;
  }
  return `${head}<div class="card stack"><span class="title">Review</span><span class="sub">Counted = untagged items you scanned (+ tagged items the reader sees). Grocy = what Grocy has in the pantry now.</span>${table}
<form class="stack" data-form="apply" data-epc="stocktake">
<label class="check"><input type="radio" name="how" value="set" checked> Set Grocy to the counts (inventory correction, no fake purchases)</label>
<label class="check"><input type="radio" name="how" value="purchase"> Add missing items as purchases</label>
<label class="check"><input type="checkbox" name="gone"> Products I didn't scan are gone</label>
<div class="actions"><button class="btn primary" type="submit">Apply to Grocy</button><button class="btn" type="button" data-action="post" data-path="/stocktake/close" data-confirm="Cancel this stock-take? Grocy isn't changed.">Cancel</button></div>
</form></div>`;
}

export function grocyUntagged(state, ui) {
  const g = state.grocy_untagged;
  if (!g || !g.rows.length) return "";
  const rows = g.rows
    .map(
      (r) => `<li class="item row">${pic(r.product_id, state, ui)}<span class="grow"><span class="title">${esc(r.name)}</span><span class="sub">${fmt(r.in_grocy)} in Grocy${r.tagged ? ` · ${r.tagged} tagged` : ""}</span></span><button class="btn" type="button" data-action="post" data-path="/grocy-stock/tag" data-product_id="${r.product_id}">Tag them</button></li>`,
    )
    .join("");
  return `<div class="section-head"><h2>In Grocy, not tagged · ${g.rows.length}</h2></div><p class="sub">Stock Grocy already has in the pantry. Tagging these never adds them again.</p><ul class="list" role="list">${rows}</ul>`;
}

export function picturesCard(state) {
  const p = state.pictures;
  if (!p.available) {
    return `<p class="sub">Product pictures come from Kroger Restock, which isn’t installed (or its lookup action is missing).</p>`;
  }
  const progress = p.text ? `<p class="sub" role="status" aria-live="polite">${esc(p.text)}</p>` : "";
  const status = p.status ? `<p class="error-text">${esc(p.status)}</p>` : "";
  return `<p class="sub">Pictures are looked up by barcode in Kroger Restock and saved in Grocy. Products with a picture you chose are never changed.</p>${status}${progress}
<div class="actions"><button class="btn" type="button" data-action="post" data-path="/pictures/fetch" ${p.running ? "disabled" : ""}>${p.running ? "Fetching…" : "Fetch missing pictures"}</button></div>`;
}

export function pantryItems(state, ui) {
  const p = state.pantry;
  const count = p.linked_present
    ? `<span class="sub">${plural(p.linked_present, "item")} · ${plural(p.products, "product")}</span>`
    : "";
  const head = `<div class="section-head"><h2>In the pantry</h2>${count}</div>`;
  if (!p.items.length) {
    const msg = state.reader.online
      ? "Nothing detected yet. Tagged items show up here once they’re in the pantry."
      : "Nothing detected yet. Once the reader is online, tagged items show up here.";
    return `${head}<div class="card empty">${esc(msg)}</div>`;
  }
  const rows = p.items
    .map((item) => {
      const thumb = pic(item.product_id, state, ui);
      const since = item.since != null ? `since ${day(item.since, state.now)}` : "";
      return `<li class="item row">${thumb}<span class="grow"><span class="title">${esc(item.name)}</span><span class="sub">${esc(since)}</span></span><span class="qty" aria-label="${item.quantity} in the pantry">${item.quantity}</span></li>`;
    })
    .join("");
  return `${head}<ul class="list" role="list">${rows}</ul>`;
}

// Tags ---------------------------------------------------------------------

function linkForm(epc, ui) {
  return `<form class="inline-form" data-form="relink" data-epc="${esc(epc)}">
<label class="field"><span>Barcode or Grocy product id (#12)</span><input class="input" name="target" autocomplete="off" required></label>
<label class="check"><input type="checkbox" name="in_stock"> Already in stock (Grocy already counts it)</label>
<div class="actions"><button class="btn primary" type="submit">Link</button></div>
</form>`;
}

export function unlinkedCards(state, ui) {
  const list = state.unlinked;
  const head = `<div class="section-head"><h2>Not linked yet · ${list.length}</h2></div>`;
  if (!list.length) return `${head}<div class="card empty">Every tag the reader has seen is linked.</div>`;
  const cards = list
    .map((u) => {
      const full = ui.fullEpc.has(u.epc);
      const howOpen = ui.howTo.has(u.epc);
      const where = u.present ? " · in the pantry now" : "";
      const how = howOpen
        ? `<ol class="steps">
<li>Double-click the linking station’s button. It pulses blue.</li>
<li>Scan the product’s barcode.</li>
<li>Hold this tag near the station until it turns green.</li>
</ol>${linkForm(u.epc, ui)}`
        : "";
      return `<div class="card stack">
<div class="row"><button class="epc grow" type="button" data-action="toggle-epc" data-epc="${esc(u.epc)}" aria-label="Tag ${esc(u.epc)}${full ? "" : ", tap to show the full code"}">${esc(full ? u.epc : shortEpc(u.epc))}</button><span class="sub">${esc(ago(u.last_seen, state.now))}</span></div>
<span class="sub">Arrived ${plural(u.arrivals, "time")}${esc(where)}</span>
<div class="actions"><button class="btn primary wide" type="button" data-action="toggle-howto" data-epc="${esc(u.epc)}" aria-expanded="${howOpen}">How to link</button><button class="btn" type="button" data-action="post" data-path="/forget" data-epc="${esc(u.epc)}">Forget</button></div>
${how}
</div>`;
    })
    .join("");
  return head + cards;
}

export function linkedList(state, ui) {
  const q = ui.search.trim().toLowerCase();
  const all = state.links;
  const shown = q
    ? all.filter((l) => l.product_name.toLowerCase().includes(q) || l.epc.toLowerCase().includes(q))
    : all;
  if (!all.length) return `<div class="card empty">No linked tags yet. Use the linking station to link one.</div>`;
  if (!shown.length) return `<div class="card empty">No tag matches “${esc(ui.search)}”.</div>`;
  const rows = shown
    .map((l) => {
      const open = ui.menu === l.epc;
      const seen = l.last_seen != null ? `seen ${day(l.last_seen, state.now)}` : "not seen yet";
      const menu = open
        ? `<div class="menu" id="menu-${esc(l.epc)}">
<button class="btn" type="button" data-action="confirm-unlink" data-epc="${esc(l.epc)}" data-name="${esc(l.product_name)}">Unlink</button>
${linkForm(l.epc, ui).replace("Link</button>", "Relink</button>")}
</div>`
        : "";
      return `<li class="item"><div class="row">${pic(l.product_id, state, ui)}<span class="grow"><span class="title">${esc(l.product_name)}${l.present ? ' <span class="sub">· in the pantry</span>' : ""}</span><span class="sub">${esc(shortEpc(l.epc))} · ${esc(seen)}</span></span><button class="btn icon-only" type="button" data-action="toggle-menu" data-epc="${esc(l.epc)}" aria-expanded="${open}" aria-label="More for ${esc(l.product_name)} (${esc(shortEpc(l.epc))})">${icons.more}</button></div>${menu}</li>`;
    })
    .join("");
  return `<ul class="list" role="list">${rows}</ul>`;
}

export function failedOps(state) {
  const ops = state.failed_ops;
  if (!ops.length) return "";
  const rows = ops
    .map((op) => {
      const buttons = op.needs_review
        ? `<button class="btn primary" type="button" data-action="post" data-path="/consume-now" data-op="${op.id}">Consume now</button><button class="btn" type="button" data-action="post" data-path="/dismiss-op" data-op="${op.id}">Dismiss</button>`
        : `<button class="btn primary" type="button" data-action="post" data-path="/retry" data-op="${op.id}">Retry</button>`;
      const what = op.kind === "add" ? "Add" : "Consume";
      const review = op.needs_review ? " · check Grocy first: it may already be done" : "";
      return `<li class="item stack"><span class="grow"><span class="title">${what} ${esc(op.product)}</span><span class="sub">${esc(op.error || "failed")}${esc(review)}</span></span><div class="actions">${buttons}</div></li>`;
    })
    .join("");
  return `<div class="section-head"><h2>Failed Grocy updates · ${ops.length}</h2></div><ul class="list" role="list">${rows}</ul>`;
}

export function needsProduct(state) {
  const list = state.needs_product;
  if (!list.length) return "";
  const grocy = state.grocy.ui_url;
  const rows = list
    .map(
      (n) => `<li class="item stack"><span class="grow"><span class="title">${esc(n.barcode)}</span><span class="sub">Scanned ${plural(n.scans, "time")} · ${esc(ago(n.last_seen, state.now))}</span></span><div class="actions"><a class="btn primary" href="${esc(grocy)}/product/new" target="_blank" rel="noopener">Create in Grocy</a><button class="btn" type="button" data-action="post" data-path="/dismiss" data-barcode="${esc(n.barcode)}">Dismiss</button></div></li>`,
    )
    .join("");
  return `<div class="section-head"><h2>Barcodes without a Grocy product · ${list.length}</h2></div><ul class="list" role="list">${rows}</ul>`;
}

// Settings -----------------------------------------------------------------

export function doorSource(state) {
  const d = state.door;
  if (!d.ha_available) return `<p class="error-text">Home Assistant isn’t reachable from the app, so sensors can’t be listed.</p>`;
  if (d.source === "options") {
    return `<p class="sub">In use: <strong>${esc(d.entity_id)}</strong>, set in the app’s Configuration tab (<code>door_entity</code>). It overrides the choice here; clear it there to use this list.</p>`;
  }
  if (!d.entity_id) return `<p class="sub">No sensor chosen yet. Until you pick one, Pantry Hub checks once an hour.</p>`;
  return "";
}

export function pickerList(cands, ui, current) {
  if (ui.candError) return `<p class="error-text">Couldn’t list Home Assistant sensors: ${esc(ui.candError)}</p>`;
  if (!cands) return `<p class="sub">Loading sensors…</p>`;
  const q = ui.pickerSearch.trim().toLowerCase();
  const match = (c) => !q || c.name.toLowerCase().includes(q) || c.entity_id.toLowerCase().includes(q);
  const opt = (c) =>
    `<li><button class="option" type="button" role="option" data-action="pick-door" data-entity="${esc(c.entity_id)}" aria-selected="${c.entity_id === current}"><span class="grow"><span class="title">${esc(c.name)}</span><span class="sub">${esc(c.entity_id)}</span></span><span class="state">${esc(c.state)}</span></button></li>`;
  const doors = cands.filter((c) => c.door_like && match(c));
  const others = cands.filter((c) => !c.door_like && match(c));
  let html = "";
  if (doors.length) html += `<li class="group" role="presentation">Doors, openings and windows</li>${doors.map(opt).join("")}`;
  if (others.length) html += `<li class="group" role="presentation">Other binary sensors</li>${others.map(opt).join("")}`;
  if (current && !cands.some((c) => c.entity_id === current)) {
    html = `<li class="group" role="presentation">Saved (not found in Home Assistant)</li>${opt({ entity_id: current, name: current, state: "missing" })}${html}`;
  }
  if (!html) return `<p class="sub">No sensor matches “${esc(ui.pickerSearch)}”.</p>`;
  return `<ul class="picker-list" role="listbox" aria-label="Door sensors">${html}</ul>`;
}

export function liveDoor(door) {
  if (!door || !door.entity_id) {
    return `<div class="live unknown" role="status"><span class="label">Right now</span><span class="big">NO SENSOR</span><span class="raw">Pick a sensor above</span></div>`;
  }
  if (door.missing) {
    return `<div class="live unknown" role="status"><span class="label">Right now</span><span class="big">NOT FOUND</span><span class="raw">${esc(door.entity_id)} isn’t in Home Assistant</span></div>`;
  }
  const word = door.door === "open" ? "OPEN" : door.door === "closed" ? "CLOSED" : "UNKNOWN";
  const cls = door.door === "open" ? "open" : door.door === "closed" ? "" : "unknown";
  const raw = `Sensor says “${esc(door.raw ?? "nothing")}”${door.inverted ? ", reversed" : ""} · updates every second`;
  return `<div class="live ${cls}" role="status" aria-live="polite"><span class="label">Right now</span><span class="big">Door: ${word}</span><span class="raw">${raw}</span></div>`;
}

export function checkStep(ui, door) {
  const c = ui.check;
  const disabled = !door || !door.entity_id || door.missing;
  if (c.phase === "idle") {
    return `<div class="checkbox-card"><span class="title">Is it reading the right way round?</span><span class="sub">Close the pantry door, tap Check it, then open the door. We’ll compare and offer to fix it if it’s backwards.</span><button class="btn primary" type="button" data-action="check-start" ${disabled ? "disabled" : ""}>Check it</button></div>`;
  }
  if (c.phase === "waiting") {
    const msg = c.changedAt ? "Hold it open a moment…" : "Open the pantry door now…";
    return `<div class="checkbox-card" role="status" aria-live="polite"><span class="title">${msg}</span><div class="actions"><button class="btn" type="button" data-action="check-cancel">Cancel</button></div></div>`;
  }
  if (c.phase === "right") {
    return `<div class="checkbox-card" role="status"><span class="title">Looks right ✓</span><span class="sub">The sensor said open when you opened the door.</span><div class="actions"><button class="btn" type="button" data-action="check-cancel">Done</button></div></div>`;
  }
  if (c.phase === "reversed") {
    return `<div class="checkbox-card" role="status"><span class="title">This sensor reports the opposite. Flip it?</span><span class="sub">It said closed while the door was open.</span><div class="actions"><button class="btn primary" type="button" data-action="check-flip">Flip</button><button class="btn" type="button" data-action="check-cancel">Not now</button></div></div>`;
  }
  if (c.phase === "flipped") {
    return `<div class="checkbox-card" role="status"><span class="title">Flipped ✓</span><span class="sub">Open and closed are now the right way round.</span><div class="actions"><button class="btn" type="button" data-action="check-cancel">Done</button></div></div>`;
  }
  return `<div class="checkbox-card" role="status"><span class="title">Didn’t see the door change</span><span class="sub">Is this the right sensor, and is it online? Try again with the door closed first.</span><div class="actions"><button class="btn primary" type="button" data-action="check-start">Try again</button><button class="btn" type="button" data-action="check-cancel">Cancel</button></div></div>`;
}

export function readerOptions(state) {
  const o = state.options;
  const ports = o.reader_antennas.length === 1 ? `Port ${o.reader_antennas[0]}` : `Ports ${o.reader_antennas.join(", ")}`;
  const conn = o.reader_connection === "persistent" ? "Persistent" : "Per window";
  const mode = o.reading_mode === "door" ? "Door-driven" : "Continuous";
  return `<dl class="opts"><dt>Reading</dt><dd>${mode}</dd><dt>Antennas</dt><dd>${esc(ports)}</dd><dt>Power</dt><dd>${esc(o.reader_power_dbm)} dBm</dd><dt>Connection</dt><dd>${conn}</dd></dl>`;
}

export function resyncCard(state) {
  const r = state.resync;
  let body = `<p class="sub">Compares tagged items in the pantry with Grocy’s stock. It only reports differences; fix them in Grocy.</p>`;
  if (r) {
    const diffs = r.differences.length
      ? `<dl class="kv">${r.differences.map((d) => `<dt>${esc(d.product)}</dt><dd>${esc(d.present_tags)} tagged · Grocy ${esc(d.grocy_amount)}</dd>`).join("")}</dl>`
      : "";
    const err = r.error ? `<p class="error-text">${esc(r.error)}</p>` : "";
    body += `<p class="sub">Last run ${esc(ago(r.checked_at, state.now))}: ${plural(r.checked, "product")}, ${plural(r.differences.length, "difference")}.</p>${err}${diffs}`;
  }
  return `${body}<div class="actions"><button class="btn" type="button" data-action="post" data-path="/resync">Compare with Grocy</button></div>`;
}

// Talking to the Pantry Hub server: boot data, JSON GET/POST, live polling.

/** Boot data the server put on #app: ingress base path, CSRF token, version. */
export function readBoot() {
  const el = document.getElementById("app");
  return JSON.parse(el.dataset.boot || "{}");
}

export class Api {
  constructor(boot) {
    this.base = (boot.base || "").replace(/\/$/, "");
    this.csrf = boot.csrf || "";
  }

  /** Every URL is built from the ingress prefix (X-Ingress-Path). */
  url(path) {
    return `${this.base}${path}`;
  }

  async get(path) {
    const res = await fetch(this.url(path), {
      cache: "no-store",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  }

  /** POST an action; resolves to {ok, message}. */
  async post(path, fields = {}) {
    const body = new URLSearchParams();
    body.set("csrf", this.csrf);
    for (const [k, v] of Object.entries(fields)) body.set(k, String(v));
    const res = await fetch(this.url(path), {
      method: "POST",
      cache: "no-store",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": this.csrf, Accept: "application/json" },
      body,
    });
    let data = null;
    try {
      data = await res.json();
    } catch {
      data = null;
    }
    if (!data) return { ok: false, message: `HTTP ${res.status}` };
    return data;
  }
}

export const VISIBLE_MS = 3000;
export const HIDDEN_MS = 30000;

/**
 * Polls a fetch function: every 3 s while the page is visible, every 30 s
 * while hidden, and at once when the page becomes visible again, comes back
 * from the back-forward cache (pageshow persisted), gets focus or the
 * network returns. This is what keeps the HA companion app from showing a
 * stale page after opening Pantry Hub from the side menu.
 */
export class Poller {
  constructor(fetchFn, { onData, onError, visibleMs = VISIBLE_MS, hiddenMs = HIDDEN_MS } = {}) {
    this.fetchFn = fetchFn;
    this.onData = onData || (() => {});
    this.onError = onError || (() => {});
    this.visibleMs = visibleMs;
    this.hiddenMs = hiddenMs;
    this.timer = null;
    this.inflight = null;
    this.failures = 0;
    this.lastStart = -Infinity;
    this.count = 0; // fetches started (tests read it)
    this.stopped = true;
    this._now = (why) => this.now(why);
    this._vis = () => (document.hidden ? this.schedule() : this.now("visible"));
    this._show = (e) => {
      if (e.persisted) this.now("pageshow");
    };
  }

  start() {
    this.stopped = false;
    document.addEventListener("visibilitychange", this._vis);
    window.addEventListener("pageshow", this._show);
    window.addEventListener("focus", this._now);
    window.addEventListener("online", this._now);
    this.now("start");
  }

  stop() {
    this.stopped = true;
    clearTimeout(this.timer);
    document.removeEventListener("visibilitychange", this._vis);
    window.removeEventListener("pageshow", this._show);
    window.removeEventListener("focus", this._now);
    window.removeEventListener("online", this._now);
  }

  schedule() {
    clearTimeout(this.timer);
    if (this.stopped) return;
    this.timer = setTimeout(() => this.tick(), document.hidden ? this.hiddenMs : this.visibleMs);
  }

  /** Fetch now (coalesced with one already running or started <250 ms ago). */
  now() {
    if (this.inflight) return this.inflight;
    if (performance.now() - this.lastStart < 250) return Promise.resolve();
    return this.tick();
  }

  async tick() {
    clearTimeout(this.timer);
    if (this.inflight) return this.inflight;
    this.lastStart = performance.now();
    this.count += 1;
    this.inflight = (async () => {
      try {
        const data = await this.fetchFn();
        this.failures = 0;
        this.onData(data);
      } catch (err) {
        this.failures += 1;
        this.onError(this.failures, err);
      } finally {
        this.inflight = null;
        this.schedule();
      }
    })();
    return this.inflight;
  }
}

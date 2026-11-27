# Pantry Hub: decisions made without asking

Decisions taken while building a release, one line of reason each, so they
can be reviewed and reverted. Newest release first.

## 0.4.1: Kroger Restock pictures + Just bought

Built against the Part A contract (`lookup_product`, `recent_received`, the
`kroger_restock_received` event) without the Kroger Restock code in hand.

1. **REST for actions, websocket for the event.** `lookup_product` /
   `recent_received` go through `POST /core/api/services/...?return_response`
   as specified; the REST base is derived from the websocket URL already in
   use (`ws://supervisor/core/websocket` → `http://supervisor/core/api`), so
   `PANTRY_HUB_HA_URL` covers development too. The event subscription is its
   own websocket connection (the door's connection only exists in door mode).
2. **"Is Kroger Restock there?"** = the action is listed by `GET /api/services`,
   cached 10 min, so installing it later is picked up without a restart.
3. **`recent_received` response shape.** Accepts `{"items": [...]}` or
   `{"received": [...]}`; the contract names the fields of an item but not the
   wrapper. One-line change if Part A uses another key.
4. **Price is per unit.** Booked as Grocy's `price` (per stock unit). If Kroger
   Restock reports line totals, divide by `quantity_received` there or here.
5. **Pictures only for barcode links.** A web relink by `#id` has no barcode,
   so no lookup. Backfill covers products Pantry Hub has linked or seen in
   Just bought, using the link's barcode or the product's first Grocy barcode.
6. **"Empty name"** also means a name equal to the barcode (what some
   lookups leave). Description is filled with "Brand · size" when empty.
7. **`pantryhub_` pictures are replaced only when Grocy lost the file**
   (backfill checks); otherwise "missing" means an empty `picture_file_name`.
8. **Not-found memory** (30 days) applies to the backfill; a fresh link still
   asks once (the person is holding the product now).
9. **Retries**: link-time picture fetch retries after 10 s, 60 s, 5 min for
   network / Home Assistant / Grocy / 5xx-429 image errors; a bad image (too
   big, not an image, 4xx) is not retried. Logs name the image host only.
10. **Resize** with Pillow 13.0.0 (musllinux wheels for amd64 and aarch64;
    adds about 5 MB to the image). Transparent PNGs are flattened on white.
11. **Picture route renamed** to `/api/picture/<id>` with
    `private, max-age=86400`; the page appends `?v=<n>`, bumped when Pantry Hub
    sets that product's picture, so a new picture shows at once. A "no
    picture" answer stays `no-store` / 204 and is re-checked server-side
    every 5 min.
12. **Station for Tag them** is the station that last sent a barcode or tag,
    else `kroger_scanner`. No station picker (one station today).
13. **One tag per tap** is enforced by the firmware (one scan per result,
    1.5 s pause), plus the hub's existing 1 s strongest-RSSI window.
14. **Skipped tags**: a tag already linked to *this* product is skipped and
    not counted; a tag linked to *another* product is skipped too (relink it
    in the web UI), with status `tag_already_linked` either way so old
    firmware shows magenta.
15. **Counted-session timeout** counts from the start or the last tap
    (60 s); "Tag them" again continues with the units left.
16. **No double count** = a "booked credit" per Not-tagging booking, used by
    the next links of that product within 7 days (station, counted session
    or web relink) as "already in stock". The credit is recorded only after
    Grocy accepted the purchase; a refused purchase reopens the row.
17. **Event log** for a matched unit: an entry `matched_booked` in the recent
    events of `/api/state` plus an info log line (the UI has no event list).
18. **Product created from a delivery** uses Grocy's first quantity unit for
    every unit field and the pantry location; the picture follows through
    the normal picture flow.
19. **Older rows** (14–30 days) are listed under a collapsed "Older" and not
    counted in "N items to tag"; rows are deleted 30 days after
    `received_at` (or arrival, if that's in the future/unparsable).
20. **Quantity capped at 99** per delivery row (guards a bad payload).
21. **Firmware 0.3.0**: `on_json_message` on `.../start`; counted state
    lives in `link_mode.h` (host-tested) like the in-stock session.

## 0.4.0: UI redesign + stale-page fix

1. **Part 1 is server-side only.** Commit 1 adds `no-store` and `GET /api/state`; the page's polling arrives with the new shell (commit 2), because the old server-rendered page was replaced in the same release. Reverting commit 2 alone leaves a working API with the old page.
2. **`tokens.css` created here, not copied.** Laundry Hub v0.2 isn't merged (its `main` is v0.1.2 and its open branch has no `frontend/tokens.css`), so the shared tokens were written from the agreed values. The file says the two copies must stay in sync; whichever lands second must match.
3. **Inter isn't downloaded.** `font-family: Inter, system-ui, …` uses Inter only where it's installed; loading Google Fonts from an ingress page would add a third-party request and break offline. To bundle it: add `inter.woff2` to `static/` with an `@font-face` rule.
4. **App palette in `app.css`.** `--ph-*` (Harvest & Clay) lives in Pantry Hub's own stylesheet; `tokens.css` holds only what Laundry Hub shares.
5. **Dark mode values.** Same hues, accents ~15% lighter (accent `#C96D37`, sage `#7C9A6B`), surfaces `#1B1814` / cards `#26221E`, picked to keep 4.5:1 text contrast.
6. **Product pictures via the app.** `GET /api/product-picture/{id}` fetches Grocy's thumbnail (`best_fit` 128 px), caches it 1 h per product in memory, and answers **204** (not 404) when there's none, so the browser logs no console error and the page shows the neutral icon.
7. **No brand/size line.** The mockup's "Hunt's · 14.5 oz" isn't data Pantry Hub has; the row shows "since Tue" (the earliest arrival of that product's tags in the current cycle).
8. **Health priority.** Antenna settings error → reader offline → reader not answered yet → Grocy unreachable → failed Grocy updates → MQTT disconnected → no / missing door sensor → dead antenna port. The banner shows the first; Details lists all.
9. **No "Grocy not set up yet" banner.** The app refuses to start without working Grocy credentials, so it can't be shown; "Can't reach Grocy" covers outages. Added "Connecting to the reader…" for the time before the reader first answers.
10. **Door check logic.** Needs a change from the starting raw state (door closed → opened) held for 3 s, then compares the interpreted state: open → "Looks right ✓", closed → "This sensor reports the opposite. Flip it?". 60 s without a change, or a sensor not reporting on/off at the start → "Didn't see the door change". Flip writes the same `door_inverted` value as 0.3.x.
11. **Picking a sensor saves immediately.** Tapping an option saves it (no separate Save button), like a native picker.
12. **Kept features not on the mockups.** Barcodes needing a Grocy product (Tags), the resync report and "Compare with Grocy" (Settings → Grocy), and linking an unlinked tag by barcode / `#id` (inside "How to link") all existed before; dropping them would remove actions.
13. **Actions answer JSON on the same paths.** `{ok, message}`; CSRF as form field `csrf` or header `X-CSRF-Token`; a stale token is 403 JSON. New `POST /settings/inverted`; `POST /settings/door` still accepts `inverted`.
14. **`/settings/door-state` → `/api/door`.** Only the old page used it.
15. **Cache busting for JS modules.** Modules import `./x.js?v=__VERSION__`; the server substitutes the version when serving, so an upgrade never mixes cached old modules with new ones.
16. **Slot re-rendering.** Each screen is split into slots re-rendered only when their HTML changes; a slot is skipped while the cursor is in one of its fields, and typed values are kept, so a 3 s poll never closes the keyboard.
17. **Browser tests.** Python Playwright 1.63.0 (matches the preinstalled Chromium build) in a new `ui` extra; `tests/test_ui_browser.py` is skipped where it isn't installed and runs in a new "UI smoke" CI job that uploads screenshots.
18. **Freshness label.** "Updated N s ago" counts from the browser's last successful fetch, so a phone with a wrong clock still shows the truth.
19. **Version in the footer of every tab**, not in the title.
20. **Recent events in `/api/state` only.** The last 20 arrivals/departures are in the API (in memory, cleared on restart) but not shown; the mockups have no place for them.
21. **Hash routing** (`#/pantry`, `#/tags`, `#/settings`) works inside the ingress iframe and survives a reload.
22. **"Check now" hidden in continuous mode**, where there are no sweeps.

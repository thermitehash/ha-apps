# Pantry Hub: decisions made without asking

Decisions taken while building a release, one line of reason each, so they
can be reviewed and reverted. Newest release first.

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

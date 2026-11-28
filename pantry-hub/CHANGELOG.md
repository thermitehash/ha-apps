# Changelog

## 0.4.3

- **Unload**: get groceries bought outside Kroger Restock (the Kroger app, in
  a store, another shop) into Pantry Hub. Start it with a **triple-click** on
  the linking station (slow green pulse) or **Start unloading** on the Pantry
  page, then scan each item. Each scan finds the Grocy product (any form of
  the barcode), or creates it from Kroger Restock's lookup (name, brand, size,
  picture), else from Grocy's barcode lookup, else lists it under "needs a
  product". Items land in **Just bought** (one row per barcode, +1 per scan)
  and work like Kroger deliveries: **Tag them** or **Not tagging** (booked
  without a price). Unload never adds anything to the Kroger cart. It ends
  after 3 minutes without a scan, with a single click, or **Stop unloading**.
- **Phone scanner**: **Scan barcode** uses the phone camera (the browser's
  barcode reader, or a bundled reader on browsers without one; nothing is
  loaded from the internet). Typing the barcode always works too. The camera
  needs Home Assistant opened over HTTPS.
- **Stock-take** for what's already in the pantry: **Start stock-take** on
  the Tags page, or hold the station button for 6 seconds (slow amber pulse).
  Scan each untagged unit once, or scan once and type how many. **Finish**
  shows a review (counted, Grocy, difference); **Set Grocy to the counts**
  books an inventory correction (no fake purchases or prices), **Add missing
  items as purchases** adds only what's missing. Products you didn't scan are
  left alone unless you tick "Products I didn't scan are gone". After
  applying, **Tag N** tags the counted items; their first reader arrival
  doesn't add them again.
- **In Grocy, not tagged** (Tags page): stock Grocy already has in the
  pantry without tags, with **Tag them**. Tagging it never adds it again.
- With Kroger Restock 0.17.3 or later, after a pickup the Just bought card
  shows "About 12 more items from your Kroger order — start unloading" (items
  Kroger Restock couldn't match), with the order's item count. It counts down
  as you unload within 6 hours of pickup, ignores products Kroger Restock
  already tracked in that order, and also appears for a pickup that happened
  while Pantry Hub was down. Older Kroger Restock versions: no prompt.
- Linking station firmware 0.4.0: triple-click Unload, hold 6 s Stock-take,
  single click leaves them; double-click (link mode) now waits 0.4 s to rule
  out a third click. Reflash to use the station for Unload / Stock-take; the
  phone scanner works without it.
- Checked again: opening Pantry Hub from the sidebar shows current data at
  once (new browser test with the page in a Home Assistant-style iframe).

## 0.4.2

Fixes the Kroger Restock integration. **0.4.1's product pictures and Just
bought matching didn't work against the real Kroger Restock (v0.17.2)**:
0.4.1 was built against a guessed contract.

- **Product pictures now work.** `kroger_restock.lookup_product` takes `upc`
  (or `product_id`), not `barcode`; 0.4.1 sent `barcode`, which Kroger Restock
  refuses (HTTP 400), so no picture was ever fetched. The scanned / Grocy
  barcode is now sent as `upc` (Kroger Restock normalizes UPC-A, EAN-13,
  UPC-E and padding itself).
- When Kroger has no product, its `reason` is used: `not_found`, `plu_code`
  or `not_a_product_barcode` → not asked again for 30 days; `rate_limited` →
  no lookups for an hour; `auth_failed` → lookups stop and Details (and the
  health list) say "Kroger Restock needs you to sign in again". Tap **Fetch
  missing pictures** after signing in to start again.
- **Just bought no longer creates duplicate Grocy products.** Kroger's `upc`
  has no check digit, so 0.4.1 never found the existing Grocy product
  (stored as the printed code, e.g. `049000028911`) and created a new one.
  Pantry Hub now uses Kroger Restock's printed `barcode` (or rebuilds the
  check digit from `upc`), and also tries the EAN-13 and Kroger 13-digit
  forms before creating a product. A product it creates gets the printed
  barcode, with the check digit. Kroger's `upc` and `product_id` are kept
  for reference.
- If 0.4.1 already created duplicates for you, merge or delete them in Grocy
  (they have a 13-digit barcode starting with `000`). Pantry Hub doesn't
  remove them.
- Rows already in Just bought from 0.4.1 get the rebuilt barcode
  automatically (database upgrade).

## 0.4.1

Needs the **Kroger Restock** integration for the new parts; without it
Pantry Hub works as before (Details says "Product pictures: Kroger Restock
not found.").

- **Product pictures**: when you link a barcode (station or web UI) and the
  Grocy product has no picture, Pantry Hub asks Kroger Restock
  (`kroger_restock.lookup_product`, only the barcode is sent), downloads the
  image, resizes it (600 px, JPEG) and stores it in Grocy as
  `pantryhub_<id>.jpg`. An empty product name or description is filled in
  too. Linking never waits for it; failures are retried in the background.
  Pictures you set yourself are never touched.
- **Fetch missing pictures** in Settings (and `button.pantry_hub_fetch_pictures`
  / `pantry_hub/pictures/press`): looks up every product Pantry Hub knows,
  one every 2 s, showing "12 of 40, 3 not found". Products Kroger has no
  picture for are skipped for 30 days.
- 40 px thumbnails in Pantry rows and linked tags, with a neutral icon when
  there's no picture.
- **Just bought**: Kroger deliveries (`kroger_restock_received` events, with
  a catch-up at start and after reconnects) show above "In the pantry" as
  "Just bought · 6 items to tag". **Tag them** puts the linking station into
  a counted link session (blue pulse; one tag per tap, "2 of 3 linked";
  already-linked tags are skipped; ends at the count or after 60 s). **Not
  tagging** removes the row and, with "Add to Grocy stock" (default), books
  the untagged units as a Grocy purchase with their price. If one of those
  units is tagged within 7 days, its pantry arrival doesn't add it again.
  Unknown barcodes get a Grocy product from Kroger's name, brand, size and
  picture. Rows fade into "Older" after 14 days and go after 30; nothing is
  ever booked automatically.
- Linking station firmware 0.3.0: accepts `pantry_hub/linking/<station>/start`
  `{"barcode", "count"}` for counted sessions. Reflash to use Tag them.
- The picture route is now `GET /api/picture/<id>` (cached for a day; the
  page adds a version so a new picture shows at once).

## 0.4.0

- **No more stale page**: opening Pantry Hub from the sidebar shows current
  data straight away, without pulling down to refresh. Pages and data are sent
  with `Cache-Control: no-store`, and the page refreshes itself every 3 s
  while visible (30 s in the background), and immediately when you come back
  to it. "Updated 3 s ago" shows how fresh it is; a banner appears if Pantry
  Hub stops answering.
- **New web UI** (same look as Laundry Hub, "Harvest & Clay" colours, light
  and dark): **Pantry** leads with one plain-language health line and what's
  in the pantry (grouped by product, with Grocy pictures); the diagnostics
  moved behind **Details**. **Tags** lists unlinked tags first, then linked
  tags (search, ⋯ menu), failed Grocy updates and barcodes without a product.
  **Settings** has a searchable door sensor picker, a big live **Door:
  OPEN/CLOSED**, and a **Check it** step that replaces the "Inverted"
  checkbox (same stored setting, so 0.3.x choices carry over).
- Every action works as before (unlink, relink, forget, dismiss, retry,
  consume now, dismiss review, resync, check now), without reloading the page.
- No change to reading, sweeps, Grocy sync or MQTT.

## 0.3.4

- Uses **aiochainway v0.4.0**: antenna/power enforcement now calls
  `UR4Client.ensure_config()` (read, write only what differs, read back,
  verify); Pantry Hub's own copy of that logic is gone. Behaviour is
  unchanged.
- **Dead port warning** (door mode): from per-antenna read counts of completed
  sweeps. With 2+ ports, a port with no reads in 5 sweeps while the others
  read 10+ times; with 1 port, no reads in 5 sweeps while tags were in the
  pantry. Raises "Antenna on port N gave no reads in 5 sweeps — check its
  cable and connection." and clears on that port's first read. Warning only.
- New `reader_activity` attributes: `per_port_reads_last_sweep`,
  `dead_ports`.

## 0.3.3

- **Reader antennas and power are enforced** (door mode). New options
  `reader_antennas` (default `[1]`) and `reader_power_dbm` (default 27,
  must be ≤ `max_reader_power_dbm` or the app refuses to start). On every
  new reader connection, before any inventory, the antenna mask and
  per-antenna power are read; if they differ they're set (antennas saved on
  the reader) and read back. A matching reader gets no writes.
- If the settings can't be applied or don't verify: no inventory,
  `reader_online` off, a "Reader antenna settings could not be applied"
  notification, and a retry on the normal schedule.
- New `reader_activity` attributes: `antennas_enabled`, `reader_power_dbm`,
  `config_applied`, `last_config_error`.
- Bench setup is `reader_antennas: [1]`; the final install with two SP11s is
  `[1, 2]`. Every enabled port must have an antenna connected.

## 0.3.2

- **Door sensor picker** in the web UI (**Settings**): a dropdown of every
  Home Assistant `binary_sensor` (door, garage door, opening and window
  sensors first) with friendly name, entity id and current state. The
  choice is stored in the app's database.
- `door_entity` in the app options still wins when set; the Settings page
  and the `reader_activity` attributes (`door_entity_source`) show which
  source is in use.
- **Door mode without a door sensor** no longer refuses to start: it runs in
  a "needs door sensor" state (sweeps every `closed_sweep_interval_s`, a
  banner in the web UI and a persistent notification linking to it).
- `door_entity` is checked when you save the configuration
  (`binary_sensor.…` or empty), so typos fail there instead of at start.
- Without a Home Assistant token (local development) the app no longer
  refuses to start; the door stays unknown.
- **Inverted** checkbox next to the door picker, for sensors that report
  `on` when closed and can't be changed at the device. The Settings page
  shows the live interpreted and raw state (`Door: open (raw: on)`).
- A saved door sensor that no longer exists in Home Assistant gets its own
  notification: "Saved door sensor … not found in Home Assistant (renamed or
  deleted?)", instead of the generic "unavailable" text.
- Fixed: switching to another door sensor while the door was already
  unknown never started the 2-minute "unavailable" notification timer.

## 0.3.1

- Uses **aiochainway v0.3.0**. Every reading window is an
  `async with client.inventory(duration=…)` block, and a sweep counts toward
  departures only if aiochainway reports it **completed** (the duration
  elapsed). Pantry Hub's own completion guesswork is gone: a reader drop is
  `connection_lost` and an abort (door, "sweep now") is `stopped`. Neither
  counts as a miss. A missing stop acknowledgement doesn't stop a sweep
  counting; it is logged.
- New option `reader_connection`: `per_window` (default) connects for each
  window with `stop_on_connect` and 3 connect retries, then disconnects.
  `persistent` keeps one connection (60 s keepalive, automatic reconnect).
- `reader_online` between windows: connect + ping + disconnect every
  10 minutes while idle, skipped if a window ran in that time.
- `sensor.pantry_hub_reader_activity` gains a `reader_connection` attribute.

## 0.3.0

- **Door-driven reading** (new default, `reading_mode: door`): Pantry Hub
  drives the UR4 in **command mode over TCP** (aiochainway
  `inventory()`), from your pantry door sensor (`door_entity`, a Home
  Assistant `binary_sensor`). Door opens → continuous reading for
  `open_continuous_s` (600 s), then a `sweep_s` (45 s) sweep every
  `open_sweep_interval_s` (300 s). Door closes → one sweep, then stop.
  Closed → a sweep every `closed_sweep_interval_s` (3600 s). The reader's
  trigger mode is never used. The old auto/UDP behaviour stays available as
  `reading_mode: continuous`.
- **Departures from sweeps**: a present item departs after being missing
  from `depart_after_missed_sweeps` (2) completed sweeps in a row. A sweep
  only counts if the reader stayed online for the whole window; aborted
  sweeps never count as misses. The `depart_after_s` timer now counts only
  continuous reading time (paused while the reader is idle).
- **Door sensor unavailable**: falls back to the closed-door sweep schedule
  (never treated as open) and raises a persistent notification in Home
  Assistant, dismissed when the sensor reports again.
- **Restart**: one sweep at start; nothing departs until 2 sweeps have
  completed.
- New entities: `sensor.pantry_hub_reader_activity` (idle / continuous /
  sweep), `sensor.pantry_hub_last_sweep`, `button.pantry_hub_sweep_now`.
- The reader's MAC (from its UDP 1111 announcements) is added to the device
  identifiers; fallback `host:port`.
- **Breaking**: `door_entity_topic` (MQTT) is replaced by `door_entity` (a
  Home Assistant entity); the app now uses the Home Assistant API.
- **In-stock session at the linking station** (mode card): print
  `docs/mode-card-in-stock.pdf`. In link mode, scan it to start an
  "already in stock" session: the pulse turns from dim blue to dim amber and
  every link is sent with `"in_stock": true`. Scan it again, double-click,
  or leave link mode idle for 60 s to end it. Needs the linking station
  firmware 0.2.0.

## 0.2.0

- **Already in stock** (existing stock): link a tag with "already in stock"
  (web UI checkbox, or `"in_stock": true` in the linking station's barcode or
  tag message) when Grocy already counts that unit. The add for its current
  arrival (or the next one, if the tag isn't in the pantry yet) is skipped;
  when it leaves, one unit is still consumed. Later arrivals behave normally.
  Off by default.
- **No double consume**: a consume that may have reached Grocy but has no
  stock entry id to check is no longer sent again. It's marked for review,
  counted in the Grocy queue sensor (`failed`, plus a new `review`
  attribute), listed in the web UI with **Consume now** / **Dismiss**, and
  included in the resync report.
- **Item left before its add reached Grocy**: before a queued (or retried)
  add is replayed, the hub checks that the item is still in the pantry. If
  that arrival has already departed, the add and its consume are both
  cancelled (net zero) and logged.
- Database schema 2 (adds two columns; a 0.1.0 database is migrated
  automatically).

## 0.1.0

First release.

- Chainway UR4 auto-mode UDP reads (via `aiochainway` 0.2.0) drive a per-tag
  presence state machine: unknown → arriving → present → missing → departed.
- Linked tags add one unit to Grocy when they arrive in the pantry and
  consume that same unit when they leave. Changes go through a journal that
  survives restarts, retries with backoff, and never adds an arrival twice.
- Linking over MQTT: scan a barcode on the linking station (the reflashed
  Kroger wall scanner in link mode), hold the tagged item near its RFID
  unit, done.
- Home Assistant entities over MQTT discovery: reader online, present tags,
  unlinked tags, Grocy queue, last link, resync differences, item event,
  resync button.
- Ingress web UI: status, links (unlink/relink), unlinked tags, barcodes that
  need a Grocy product, failed operations with retry.

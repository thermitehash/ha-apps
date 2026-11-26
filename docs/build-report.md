# Pantry Hub build report
- App version: 0.2.0 (0.1.0 was never merged/released; both are in PR #1)
- aiochainway version used: v0.2.0 (`f188a02`, ships `aiochainway.testing.FakeUR4`)
- Date / PR / Commit / CI (per job): 2026-11-26 / [thermitehash/ha-apps#1](https://github.com/thermitehash/ha-apps/pull/1) / code verified at `de48568` (this report is committed on top) / CI on `de48568`, all green:
  - Lint: Ruff + mypy ✅ · HA app linter ✅ · yamllint ✅ · ESPHome linking station (C++ host tests, `esphome config`, `esphome compile`) ✅ · Version bump (0.2.0) ✅
  - Test: Pytest ubuntu-latest ✅ · Pytest macos-latest ✅ (coverage gate 90%)
  - Build: Prepare ✅ · Build amd64 (native, smoke test) ✅ · Build aarch64 (native, smoke test) ✅ · Publish manifest ⏭ skipped (PR; publishes on merge to main)

## Summary
**v0.2.0** applies the open-question decisions:
- **Q1 "already in stock"**: per-link option (web UI checkbox; `"in_stock": true` in the station's barcode or tag message). It skips the add of the current presence cycle (or the next arrival if the tag isn't in the pantry), only that cycle. Default off. The departure of that cycle still consumes one unit, by amount at the Pantry location, because Grocy counted it.
- **Q2 ambiguous consume without a stock entry id**: never resent. It's failed for review (`resolution = review`), counted in `sensor.pantry_hub_grocy_queue` (`failed`, plus a new `review` attribute), listed in the web UI with **Consume now** / **Dismiss**, and included in the resync report (`review_ops`).
- **Q3 add replayed after the item left**: before replaying any queued or retried add, the worker checks the EPC's presence. If that cycle has departed, it cancels both the add and its consume (net zero) and logs it. If the add was ambiguous, it first checks Grocy: a landed add is kept, and the queued consume removes it. If nothing has landed, the add is cancelled and the delayed duplicate check removes a late landing.
- Schema v2 (`links.skip_add_cycle`, `ops.resolution`), with an automatic add-only migration from v1.

**v0.1.0** (same PR) built in one pass on `claude/kind-ritchie-58v2e4`, one PR:
- **Pantry Hub app** (`pantry-hub/`, slug `pantry_hub`): asyncio Python 3.14 service. Chainway UR4 UDP auto-mode reads via `aiochainway` → per-EPC presence state machine → Grocy add/consume through a persistent, idempotent SQLite journal; MQTT linking workflow; MQTT discovery entities; ingress web UI rendered on the server.
- **Linking station firmware** (`esphome/linking-station/`): the Kroger wall scanner (AtomS3 Lite + QRCode2) reflashed with a U107 on Grove. Kroger mode is unchanged; double-click enters link mode. The U107 driver is an ESPHome external component (`jrd4035`) with host-tested protocol code.
- **Docs**: `docs/setup.md`, `docs/automations.md`, `docs/tagging.md`, `pantry-hub/DOCS.md`.
- **CI**: lint (ruff 0.17.6, mypy --strict, HA app linter, yamllint, esphome config + compile, C++ host tests, version bump), tests (ubuntu + macOS, coverage gate 90%), multi-arch build with container smoke test and GHCR publish.
- No hardware was available: everything is tested against the `aiochainway` fake reader, a fake Grocy HTTP server and an in-process MQTT broker (`amqtt`).

## Grocy API calls used (endpoint, purpose, verified against Grocy version)
All checked against `grocy.openapi.json` on Grocy `master` (latest release **v4.7.1**). Auth header `GROCY-API-KEY`.

| Endpoint | Purpose |
|---|---|
| `GET /api/system/info` | Start-up check of URL + key (401 → refuse to start) |
| `GET /api/objects/locations/{id}` | Start-up check that `grocy_location_id` exists |
| `GET /api/stock/products/by-barcode/{barcode}` | Linking lookup (400 = unknown barcode) |
| `GET /api/stock/barcodes/external-lookup/{barcode}?add=true` | Lookup-plugin fallback; creates the product, returns `id` |
| `GET /api/stock/products/{id}` | Product name + `default_best_before_days` (relink by id; after external lookup) |
| `GET /api/stock/products/{id}/entries` | Idempotency checks after an ambiguous failure (note marker / stock_id) and resync amounts per location |
| `POST /api/stock/products/{id}/add` | Arrival: `amount 1, transaction_type purchase, location_id, best_before_date?, note "Pantry Hub op N EPC …"`; `stock_id` from the response is stored |
| `POST /api/stock/products/{id}/consume` | Departure: `amount 1, transaction_type consume, stock_entry_id` (falls back to `location_id` when no stock id is known) |

## Presence state machine (final thresholds and behaviour)
- `unknown → arriving` on the first read; `arriving → present` once **`arrive_reads` (3)** reads land within a sliding **`arrive_window_s` (10 s)** → emits **arrived**. An arriving tag whose window empties goes back to `unknown`.
- `present → missing` after **min(60 s, depart_after_s/2)** unseen (informational; no event). A read in `missing` → `present`, no event.
- `missing → departed` after **`depart_after_s` (900 s)** unseen → emits **departed**; the tag is forgotten, and a later read starts a new arrival.
- Door gating (`depart_requires_door_closed`): departures only fire while the door is **closed and has been closed ≥ arrive_window_s** (time to re-read tags shadowed by a person). Door unknown (no message yet) blocks departures. Payloads: `ON/OFF`, `open/closed`, `true/false`, `1/0` (ON = open), zigbee2mqtt `{"contact": true}` (= closed), `{"state": …}`.
- Restart: EPCs persisted as `present` are restored as present with last-seen = start time, so nothing departs for one full `depart_after_s`. `arriving` isn't persisted (re-reads redo it).
- Late-landing protection: an add that may have reached Grocy more than once (timeout / 5xx / connection reset, then retried) is re-checked `verify_after` (60 s) later; duplicate stock entries carrying the same op marker are consumed again. Pending checks survive restarts and are counted as `verifying`.
- Q1: an arrival whose cycle equals the link's `skip_add_cycle` journals a *skipped* add (status done, resolution `in_stock`, nothing sent). The departure then journals a normal consume, by amount, with no stock entry id.
- Q3: replaying an add first compares its cycle with the persisted presence: `presence.cycle > op.cycle` or (same cycle and `departed`) → cancel add + consume (resolution `cancelled`).
- Q2: an ambiguous consume with no stock entry id → `failed` + resolution `review`; it can't be blindly retried (Retry is refused). Only **Consume now** (by amount) or **Dismiss** closes it.
- Journal rules: an arrival journals one add per (EPC, arrival cycle) in the same SQLite transaction as the presence update (`dedupe_key = epc:cycle:add`). A departure journals a consume only if that cycle's add was journaled and didn't fail. Linking a tag that is already present journals that cycle's add once.

## Linking flow (final topics and payloads)
- `pantry_hub/linking/<station_id>/barcode` ← `{"barcode": "...", "ts": <epoch s>, "in_stock"?: true}` (`station_id` matches `[A-Za-z0-9_-]{1,32}`; the firmware uses `kroger_scanner`)
- `pantry_hub/linking/<station_id>/tag` ← `{"epc": "<hex>", "rssi": -45.2, "ts": ..., "in_stock"?: true}` (`in_stock` on either message marks the new link "already in stock"; the `linked` result message says so)
- `pantry_hub/linking/<station_id>/result` → `{"status": "linked|unknown_barcode|tag_already_linked|timeout|error", "product": "...", "message": "...", "barcode"?: "...", "epc"?: "..."}`
- Flow: by-barcode lookup → else external lookup `?add=true` → else `unknown_barcode` + "needs product" list. The first tag must arrive within **15 s** of the barcode; then tags are collected for **1 s** and the strongest RSSI wins (unknown RSSI loses; ties keep the first). Tags after that window are ignored. Already linked → `tag_already_linked` with the existing product (relink only in the web UI). A new barcode on the same station replaces the session. A tag with no session → `error` "Scan a barcode first".
- Linking never changes Grocy stock.
- `pantry_hub/resync/result` also carries `review_ops: [{op_id, product_id, product, epc, note}]`.
- Also published: `pantry_hub/events` (`{"event_type": "arrived|departed", "epc", "product", "product_id", "linked", "ts"}`), `pantry_hub/state` (retained), `pantry_hub/last_link` (retained), `pantry_hub/resync/result` (retained), `pantry_hub/status` (`online`/`offline`, LWT). Subscribed: `pantry_hub/resync/press`, `homeassistant/status`, `door_entity_topic`.

## Linking station firmware (pins, UARTs, U107 power, mode behaviour, Kroger Restock event/action used)
- Device name **`kroger-scanner`** (reuses the wall scanner, per decision Q3 = b); HA action `esphome.kroger_scanner_feedback` unchanged.
- QRCode2: **G5** RX (base TX → Atom), **G6** TX, **G7** TRIG (unused, auto mode), **G8** LED (driven by M14; unused), UART 115200. U107: Grove **G1** RX / **G2** TX (swap if no reads), UART 115200, 5 V from USB-C. Logs over USB-CDC (`hardware_uart: USB_SERIAL_JTAG`).
- U107 (JRD-4035): TX power **15 dBm** default (`u107_tx_power_dbm`, schema 5–26), region US. Single-poll every 100 ms during a scan; strongest tag after the first tag + 1 s; scan up to 15 s. The module is sent to **sleep** at boot and after every scan, so the radio only transmits during link-mode scans (5 V stays on: a hardware limit).
- Modes: **Kroger** (default, LED off when idle, existing result colours) → barcode calls **`kroger_restock.scan_upc`** with `upc` + **`source: esphome`** (from `ha-kroger-restock` `esphome/kroger-scanner-atoms3.yaml` / `services.yaml`). **Link**: double-click toggles; slow dim blue pulse; auto-exit after 60 s idle; exit shows a 250 ms cyan flash, then LED off. Results: green linked, amber unknown barcode, magenta already linked, red error/timeout.
- Button: double-click = link mode; short press (after ~350 ms) = `kroger_restock.print_list`; hold 1–6 s = `kroger_restock.sync`.
- Beep: switch "Link result beep (unverified)", default off; sends `m14_beep_cmd` (default `21 63 42 01`, the M14 "beep on decode" command) 1/2/3×. Marked unverified.
- MQTT is used for the linking topics only (`discovery: false`); the native API is kept for Kroger mode.

## HA entities created
`binary_sensor.pantry_hub_reader_online`, `sensor.pantry_hub_present_tags`, `sensor.pantry_hub_unlinked_tags`, `sensor.pantry_hub_grocy_queue` (attributes: failed (includes review), review, online, last_error), `sensor.pantry_hub_last_link`, `sensor.pantry_hub_resync_differences` (added: holds the resync report), `event.pantry_hub_item` (`arrived`/`departed`), `button.pantry_hub_resync`. All under one device "Pantry Hub" via device-based discovery (`homeassistant/device/pantry_hub/config`) using `default_entity_id`.
Linking station adds: `binary_sensor` Link mode, `text_sensor` Last linked tag, `switch` Link result beep (unverified).

## Spec changes vs. this spec
v0.2.0:
- Q1 interpretation: skipping the add means the tag *owns* the unit Grocy already counted for that cycle, so its departure still consumes one unit, by amount at the Pantry location. Without that, Grocy would never decrease for existing stock. If the tag isn't in the pantry when linked, the skip applies to its next arrival (the "current" cycle hasn't started).
- Q1 linking station firmware: not changed; it doesn't send `in_stock` yet. Use the web UI (or any MQTT client) for existing stock. A station gesture can be added later.
- Q2: a review consume blocks nothing (it's failed, not pending). Retry is disabled for it on purpose.
- Q3 also covers the Grocy-down case (arrival and departure both queued): both are cancelled without calling Grocy. An ambiguous add is checked in Grocy before cancelling.

v0.1.0:
1. **aiochainway v0.2.0** instead of v0.1.0 (your instruction); it ships `aiochainway.testing`.
2. **Linking station = reflashed Kroger wall scanner** (device `kroger-scanner`, decisions Q3–Q7): double-click for link mode; Kroger idle LED stays off (not solid white); link mode = blue pulse; magenta = already linked; beep switch unverified/off; housing out of scope.
3. **Image naming / builder**: the official HA builder is now the `home-assistant/builder/actions/*` set (the old `home-assistant/builder` action is deprecated). Per-arch images are `ghcr.io/thermitehash/{arch}-pantry-hub` plus a multi-arch manifest **`ghcr.io/thermitehash/pantry-hub`** (`image:` in config.yaml), instead of `pantry-hub-{arch}`. Builds run **natively** (ubuntu-24.04 / ubuntu-24.04-arm), not under QEMU; the smoke test runs each arch natively.
4. **`reader_udp_port` option dropped**: with bridge networking the container port is fixed by `ports:` in config.yaml (8888/udp). The host port is changed under the app's **Network** tab (HA's own mechanism). The hub always binds 8888 (`PANTRY_HUB_UDP_PORT` for local dev).
5. **Extra options**: `reader_tcp_probe` (bool, default false: the "optional TCP get_version probe"), `reader_offline_after_s` (120), `grocy_ui_url` (browser base URL for links in the web UI; internal `grocy_url` isn't reachable from a browser).
6. **Extra entity** `sensor.pantry_hub_resync_differences` to surface the resync report.
7. **`aioresponses` not used**: 0.7.9 is incompatible with aiohttp 3.14 (`ClientResponse.__init__` missing `stream_writer`). The Grocy API is mocked with a stateful fake Grocy **HTTP server** (aiohttp, fault injection) plus a scripted server for status/timeout mapping.
8. **Unlinked list**: an EPC enters it on a debounced *arrival* (not on any single read), so stray reads of neighbouring tags don't fill it. The sensor counts unlinked tags currently in the pantry.
9. `max_reader_power_dbm` is validated (0–27; >27 refused) and documented for provisioning. The hub never configures the reader, so there's nothing for it to enforce at runtime.
10. `startup: application`, `boot: auto` and `ingress_port: 8099` are **left out of config.yaml**: they're HA's defaults (same behaviour), and the HA app linter (also required by the spec) fails on keys set to default values.
11. Default `grocy_url` = `http://a0d7b954-grocy`. It requires enabling the Grocy app's port (Grocy's direct server only runs when a host port is set); documented.

## Open questions
Q1–Q3 are resolved in v0.2.0 (above). Q4–Q7 stay as first-run hardware checks (no code changes):
4. **U107 TX power floor**: JRD-4035/M100 modules often accept only ~18.5–26 dBm. If 15 dBm is rejected, the firmware logs a warning. Confirm on hardware and raise the default if needed.
5. **U107 Grove TX/RX orientation** (G1/G2) is unverified; swap if no reads.
6. **UDP source IP through the Supervisor's Docker bridge**: expected to be preserved (DNAT); if `dropped_sources` rises, NAT is rewriting it.
7. HA 2026.11 MQTT discovery `default_entity_id` (replacement for `object_id`): verify the entity IDs on first run.

New: 8. Should the linking station get a gesture for "already in stock" (e.g. triple-click before scanning), so existing stock can be tagged without the web UI?

## Hardware/setup impact (ports, firewall, disk, power supply)
- **Ports**: UDP **8888** on 192.168.1.38 (UR4 → hub, host port configurable under Network); ingress 8099 internal; no host networking.
- **Firewall**: allow UDP reader IP → 192.168.1.38:8888; station → Mosquitto TCP 1883; optional TCP HA → reader:8888 if `reader_tcp_probe`.
- **Disk**: free space first (86% full). The image is a small Alpine Python image; the SQLite DB is tiny. State is republished on change, with counters at most once a minute, to keep the recorder small.
- **Grocy app** needs its port enabled and ssl off (or certificates) for `http://a0d7b954-grocy`.
- **Power**: linking station needs **5 V ≥ 2 A USB-C** (QRCode2 + U107). Reader conducted power **≤ 27 dBm** (8.3 dBi SP11, FCC 36 dBm EIRP).
- U107 needs mounting beside the scanner, antenna facing out (housing doesn't fit; separate ha-kroger-restock PR later).

## Test results (tests, coverage, property tests, fault injection)
- **149 pytest tests** pass locally (Python 3.14.6, Linux; two full runs); CI runs ubuntu-latest and macos-latest. **Coverage 95%** (branch) for `pantry_hub` (gate 90%): `__main__` 98, `grocy` 96, `hub` 94, `linking` 94, `store` 93, `web` 99, `worker` 91, `mqtt` 86, `presence`/`config`/`discovery` 100.
- **v0.2.0 regression tests** (`tests/test_v020_decisions.py`, `tests/test_worker.py`):
  - Q1: in-stock link while present (no add; departure consumes the pre-existing unit; next arrival adds); while absent (next arrival skipped only); default off; relink in-stock doesn't undo a queued add; MQTT `in_stock` on the barcode and on the tag message; web UI checkbox end to end.
  - Q2: lost-answer consume without a stock id → review, sent exactly once; Retry refused; Dismiss; Consume now (worker and web); a crash mid-request → review; consumes *with* a stock id keep the existing check; review count in the MQTT state and `review_ops` in the resync report; under-consume (not double) on 5xx.
  - Q3: failed add retried after departure → cancelled, nothing sent, the next arrival is added; arrival + departure queued while Grocy is down → both cancelled, no calls; an ambiguous add that landed is kept and consumed; an ambiguous add not found → cancelled, and a late landing is removed by the duplicate check.
  - Schema v1 → v2 migration (idempotent).
  - Mutation checks: reverting Q1 → 7 tests fail; Q2 → 4; Q3 → 3.
- **Unit**: presence (thresholds, sliding window, arriving expiry, missing/departed timers, restart grace, door gating incl. settle and re-open), RSSI selection, journal idempotency, FIFO replay, backoff, config validation, door payload parsing.
- **Integration** (fake UR4 frames via `aiochainway.testing.tag_frames` over real UDP → hub → fake Grocy HTTP → `amqtt` broker): arrival/departure, unlinked, discovery + availability + HA-restart republish, full MQTT linking flow (linked, already linked, unknown barcode, timeout), link-while-present adds once, resync, relink/unlink, web UI actions + CSRF + ingress-only, TCP probe with `FakeUR4`, `run()` start/stop and SIGTERM, refuse-to-start cases.
- **Fault injection**: Grocy down (503) with ordered replay; 500, apply-then-500, apply-then-hang, hang-before-apply on add *and* consume (no double add/consume); auth errors; rejected ops; UDP garbage / short frames / wrong source; MQTT broker stop/start (reconnect, outbox, retained republish); restart mid-arrival with the add still queued.
- **Property tests (Hypothesis, CI profile 300 examples)**: (1) presence machine with random reads/ticks/door/restarts: events alternate arrived/departed per EPC, never two arrivals. (2) presence + journal + worker with random reads, links, Grocy down / lost-answer / **late-landing** faults and crash-restarts: every unit consumed at most once and only if added; no pending or failed ops left; final Grocy stock == linked tags in the pantry, **one unit each** (no double add). Mutation checks: skipping the pre-retry idempotency check → caught; disabling the delayed duplicate check → caught by the pinned `LATE_DUPLICATE` example (`Counter({'AAAA': 2})`).
- **Firmware**: `esphome config` clean (ESPHome 2026.11.1); JRD-4035 protocol host tests pass (g++ -Wall -Wextra -Werror). `esphome compile` couldn't finish in the build sandbox (dl.espressif.com blocked); CI compiles it.
- **Image**: couldn't build locally (GHCR blob host blocked in the sandbox); CI builds amd64 + aarch64 natively and smoke-tests start → MQTT connect → UDP datagram → SIGTERM → clean stop → DB written.

## Bugs found and fixed (table)
| # | Bug | Found by | Fix | Regression test |
|---|---|---|---|---|
| 1 | Broker going away while discovery (QoS 1) was being published left the hub "connected" until aiomqtt's 10 s default timeout | integration test `test_mqtt_broker_restart_reconnects_and_republishes` (flaky) | aiomqtt `timeout=5` for every MQTT operation | same test, stops the broker immediately after connect |
| 2 | Retained state republished every tick while tags were read (datagram counters changed), i.e. an MQTT message and recorder write every second | self-review | change detection ignores traffic counters; counters go out once a minute | `test_state_not_republished_for_traffic_counters_alone` |
| 3 | `aioresponses` broken with aiohttp 3.14 | test run | replaced with local scripted/fake HTTP servers | `test_grocy_client.py` |
| 4 | Template binary sensor used with `component.update` (not a PollingComponent) | `esphome config` | removed; the lambda sensor updates itself | CI `esphome config` |
| 5 | `${…}` substitution inside a YAML flow sequence broke parsing (beep bytes) | `esphome config` | lambda `return {${m14_beep_cmd}};` | CI `esphome config` |
| 6 | `rgb_order` deprecated in esp32_rmt_led_strip | `esphome config` warning | `channel_colors: GRB` | CI `esphome config` |
| 7 | Container smoke test failed on aarch64 with exit 141: `docker logs | grep -q` under `pipefail` gets SIGPIPE when grep exits early (amd64 passed by luck) | CI `Build aarch64` | logs written to a file, then grepped | CI smoke test |
| 8 | config.yaml set default-valued keys (`boot`, `startup`, `ingress_port`) | CI HA app linter | removed (defaults unchanged) | CI app linter |
| 9 | Firmware didn't compile: the pure protocol namespace `jrd4035` was ambiguous with `esphome::jrd4035` under the generated `using namespace esphome;` | CI `esphome compile` | protocol namespace renamed `jrd4035_proto` | CI `esphome compile` |
| 10 | **Double add when Grocy finishes a timed-out add after the retry** (client gives up at the timeout; the retry's note check finds nothing yet; the retry adds; the original request then lands) | macOS CI flake → reproduced with slowed departures | delayed, persisted duplicate check (`verify_after`, 60 s > 15 s HTTP timeout) for every add that may have been sent more than once: extra stock entries with the op's marker are consumed by stock entry id and logged | `test_verify_removes_late_duplicates`; property example `LATE_DUPLICATE` (fails without the check); integration `test_grocy_faults_never_double_add[timeout]` |
| 11 | Unclosed SQLite write cursors made `Store.close()` fail ("database table is locked") and left aiosqlite's thread alive, so shutdown could hang and the SIGTERM flush could fail | teardown error in repeated runs | every statement goes through helpers that close the cursor; `close()` always closes the connection (checkpoint failure only logged; data is already durable) | `test_close_after_writes_and_checkpoint_failure` |
| 12 | Test rig treated "event queue empty" as "event handled" (race on slow runners) | macOS CI | `task_done()` in the event loop + `Hub.drain_events()` (`queue.join()`) | `rig.idle()` uses it; reproduced with +0.3 s departures |
| 13 | Overflow-unsafe `millis()` comparison for the U107 wake delay | self-review | wrap-safe signed difference | — (firmware) |
| 14 | (v0.2.0, behaviour) A failed add retried after the item left would add a unit that is never consumed | open question Q3 | presence check before replay; cancel add + consume | `test_q3_*` (4 tests) |
| 15 | (v0.2.0, behaviour) An ambiguous consume without a stock id was resent, risking a double consume | open question Q2 | review instead of resend | `test_q2_*` (5 tests) |
| 16 | (v0.2.0, behaviour) Tagging existing stock double-counted it | open question Q1 | "already in stock" per-link skip | `test_q1_*` (7 tests) |

## First-run checklist when hardware arrives
1. Free HA disk space below ~75%; take a backup.
2. Mosquitto + MQTT integration up; create a `linking-station` HA user for MQTT.
3. Grocy app: enable a port, ssl off, create the **Pantry** location and an **API key**; Grocy HACS integration set up.
4. Add `https://github.com/thermitehash/ha-apps`, install Pantry Hub (check that it pulls `ghcr.io/thermitehash/pantry-hub:0.2.0`, no local build), set the options, start, and look for `Grocy … OK` / `MQTT connected` / `Pantry Hub running` in the log.
5. Check the entity IDs in HA match the list above (`default_entity_id`).
6. Provision the UR4 from the Mac with `aiochainway` v0.2.0: static IP on the IoT VLAN, region USA, power ≤ 27 dBm, auto mode, UDP destination `192.168.1.38:8888`; add the firewall rule.
7. Put one tagged item in range: `reader_online` on, `bad_frames` 0, `dropped_sources` 0 (else set `allowed_reader_ip`). An unlinked arrival appears in the web UI.
8. Flash the linking station (keep the existing API key); fit the U107 beside the scanner, antenna out; **5 V ≥ 2 A** supply. Check Kroger mode still works (scan → feedback colour).
9. Double-click → blue pulse. Scan a known product, hold its tag: green. If no tag is ever read, swap `u107_rx_pin`/`u107_tx_pin`. Watch the log for "Module rejected TX power" and adjust `u107_tx_power_dbm`.
10. Try the "Link result beep (unverified)" switch; set `m14_beep_cmd` if you find a real buzzer command.
11. Put the linked item in the pantry: Grocy +1 within ~10 s. Remove it: Grocy −1 after `depart_after_s` (temporarily set 60 s to test). Check the stock entry note "Pantry Hub op …".
12. Press **Resync**; it should report 0 differences for tagged products.
13. Restart the app with items in the pantry: nothing departs for one `depart_after_s` window.
14. Import the low-stock → Kroger Restock automation from `docs/automations.md`.
15. (v0.2.0) Tag one item Grocy already counts with **already in stock** in the web UI: on arrival, no Grocy add; on departure, Grocy −1.
16. (v0.2.0) Check the web UI's Failed operations list stays empty in normal use; if a **Review** row appears, check Grocy, then Consume now / Dismiss.

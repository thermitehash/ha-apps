# Pantry Hub

Pantry Hub turns UHF RFID reads from a Chainway UR4 in your pantry into
Grocy stock changes. When a tagged item arrives in the pantry, one unit is
added in Grocy. When it leaves, that same unit is consumed.

- **Grocy** owns products, barcodes, quantities, locations, expiry dates
  and minimum stock.
- **Pantry Hub** keeps only which tag (EPC) belongs to which Grocy
  product, which tags are in the pantry, and the linking workflow.
- **Home Assistant** runs the automations, such as low stock → Kroger
  Restock.

Full setup, including freeing disk space, Grocy and reader provisioning, is
in [docs/setup.md](https://github.com/thermitehash/ha-apps/blob/main/docs/setup.md).

## Before you start

1. **Mosquitto broker** app installed and running, plus the MQTT
   integration. Pantry Hub gets the broker credentials from the Supervisor,
   so there's no MQTT password to enter.
2. **Grocy** app installed, with:
   - a **Pantry** location (Grocy → Manage master data → Locations; note
     its ID from the URL when you edit it),
   - an **API key** (Grocy → Settings (wrench icon) → Manage API keys).
3. The Grocy app's **port enabled** so other apps can reach it: Grocy app →
   Configuration → Network → set any port (e.g. `9192`) and save, and turn
   off **ssl** in the Grocy app's options unless you've set up certificates.
   Pantry Hub talks to Grocy over Home Assistant's internal app network, so
   the port you pick doesn't matter; it just has to be enabled.

## Finding the Grocy URL

Use **`http://a0d7b954-grocy`**: the Grocy app's internal hostname
(`<repository id>-<slug>`). To check it, open the Grocy app page: the
hostname is shown in the app's info, or in the page URL as
`a0d7b954_grocy` (written with a hyphen in the hostname). If you run Grocy
elsewhere, use its normal URL, e.g. `http://192.168.1.50:9283`.

Pantry Hub checks Grocy at start (URL, API key, location). If something is
wrong it logs why, refuses to start, and the app stops. Read the Log tab.

## Options

| Option | Default | Meaning |
|---|---|---|
| `reader_host` | — | The UR4's IP address. |
| `allowed_reader_ip` | (reader IP) | Only UDP datagrams from this IP are accepted. UDP is unauthenticated, so keep this set. |
| `reader_tcp_probe` | `false` | Also ask the reader for its version over TCP every minute, so it shows online when no tags are in range. |
| `reader_offline_after_s` | `120` | No datagram for this long → reader offline. |
| `grocy_url` | `http://a0d7b954-grocy` | See above. |
| `grocy_api_key` | — | Grocy API key. |
| `grocy_location_id` | `1` | ID of the Pantry location. Arrivals are added there. |
| `grocy_ui_url` | (empty) | Base URL your browser uses for Grocy, so links in the web UI work. If empty, links use `grocy_url`, which only works inside Home Assistant's network. |
| `arrive_reads` / `arrive_window_s` | `3` / `10` | A tag must be read this many times within this window before it counts as arrived. This filters out tags carried past the door. |
| `depart_after_s` | `900` | Not read for this long → departed (one unit consumed). |
| `depart_requires_door_closed` | `false` | Only count departures while the pantry door is closed (and has been closed for one arrival window). Needs `door_entity_topic`. With this on and no door message received yet, nothing departs. |
| `door_entity_topic` | (empty) | MQTT topic of the door contact. Accepts `ON`/`OFF`, `open`/`closed`, `true`/`false`, `1`/`0` (ON = open), or zigbee2mqtt JSON `{"contact": true}` (true = closed). |
| `max_reader_power_dbm` | `27` | Ceiling for reader power. The hub doesn't configure the reader, but refuses values over 27: with 8.3 dBi antennas (Vulcan SP11) that's the FCC 36 dBm EIRP limit. Use the same ceiling when provisioning the reader with `aiochainway`. |
| `log_level` | `info` | `debug` logs every UDP datagram as hex. |

### Port

The reader sends its reads to UDP **8888** on the Home Assistant host. To
use a different host port, change it under **Network**. The container
always listens on 8888 internally. Host networking isn't needed.

## How presence works

```
unknown ──read──▶ arriving ──3 reads in 10 s──▶ present ◀──read── missing
   ▲                  │                            │ unseen 60 s ▲    │
   └── window expires ┘                            └─────────────┘    │
                                                  unseen 900 s (door closed, if required)
                                                                       ▼
                                                                    departed
```

- **Arrived** (linked tag): Grocy `add`, amount 1, at the Pantry location.
  The best-before date is the product's default best-before days (`-1`
  means never: 2999-12-31).
- **Departed** (linked tag): Grocy `consume` of that exact stock entry.
- **Unlinked tag arrived**: added to the unlinked list. Grocy isn't touched.
- **Restart**: tags that were in the pantry are treated as present for one
  full `depart_after_s` window, so a restart never consumes everything at
  once.

Every Grocy change goes through a journal in `/data/pantry_hub.db`. If
Grocy is down, changes queue up (`sensor.pantry_hub_grocy_queue`) and replay
in order. A request that may have reached Grocy before failing is checked
before it's sent again. Because Grocy can still finish a request after
Pantry Hub gave up waiting, such adds get a second check a minute later:
any duplicate unit carrying the same marker is consumed again (and logged).
An arrival never leaves more than one unit in Grocy. If Grocy rejects
a change (for example, the unit was already consumed by hand), it's marked
failed and listed in the web UI with a **Retry** button. Nothing is dropped
silently.

Two rules keep counts honest when things go wrong:

- **Item left before its add reached Grocy** (Grocy was down, or you retry
  a failed add later): before an add is replayed, Pantry Hub checks the
  item is still in the pantry. If that arrival has already departed, the
  add and its consume are both cancelled (net zero) and logged.
- **Consume that may or may not have reached Grocy**, with no stock entry
  id to check (for example an "already in stock" item, see below): it's
  never sent twice. It's marked **review** (counted as failed, plus the
  `review` attribute on `sensor.pantry_hub_grocy_queue`, and listed in the
  resync report). In the web UI, choose **Consume now** if Grocy didn't
  take it, or **Dismiss** if it did. Pantry Hub prefers one unit too many
  in Grocy over one too few.

## Linking tags

See [docs/tagging.md](https://github.com/thermitehash/ha-apps/blob/main/docs/tagging.md).
In short: on the linking station (the Kroger wall scanner), **double-click**
the button to enter link mode (LED pulses blue). Scan the barcode, then hold
the tagged item near the RFID unit. Green means linked; amber means Grocy
doesn't know the barcode; magenta means the tag is already linked; red means
an error or timeout.

Linking alone doesn't change Grocy stock. When the item goes into the pantry,
its arrival adds it. If the tag was already sitting in the pantry when you
linked it, the add happens right away, once.

### Already in stock

Tagging something Grocy already counts (stock that was on the shelf before
you started)? Link it with **already in stock**: tick the checkbox in the
web UI's Link form, or send `"in_stock": true` in the station's barcode or
tag message. The add for its current arrival (or the next one, if the tag
isn't in the pantry right now) is skipped. When it leaves, one unit is
still consumed at the Pantry location. Every later arrival is added
normally. Off by default.

### MQTT topics

| Topic | Direction | Payload |
|---|---|---|
| `pantry_hub/linking/<station>/barcode` | station → hub | `{"barcode": "...", "ts": ..., "in_stock"?: true}` |
| `pantry_hub/linking/<station>/tag` | station → hub | `{"epc": "...", "rssi": -45.2, "ts": ..., "in_stock"?: true}` |
| `pantry_hub/linking/<station>/result` | hub → station | `{"status": "linked\|unknown_barcode\|tag_already_linked\|timeout\|error", "product": "...", "message": "..."}` |
| `pantry_hub/events` | hub → anyone | `{"event_type": "arrived\|departed", "epc", "product", "product_id", "linked", "ts"}` |
| `pantry_hub/state` | hub → HA (retained) | sensor values |
| `pantry_hub/status` | hub → HA (retained, LWT) | `online` / `offline` |

## Entities

| Entity | What |
|---|---|
| `binary_sensor.pantry_hub_reader_online` | A datagram from the reader within `reader_offline_after_s` (or a TCP probe answer). Attributes: datagram/tag/bad-frame/dropped-source counters. |
| `sensor.pantry_hub_present_tags` | Tags in the pantry (present or briefly missing). |
| `sensor.pantry_hub_unlinked_tags` | Unlinked tags currently in the pantry. |
| `sensor.pantry_hub_grocy_queue` | Grocy changes waiting. Attributes: `failed` (includes review), `review`, `online`, `last_error`. |
| `sensor.pantry_hub_last_link` | Product of the most recent link; attributes: EPC, barcode, station, time. |
| `sensor.pantry_hub_resync_differences` | Products whose Grocy pantry amount differs from the linked tags present, after the last resync. |
| `event.pantry_hub_item` | `arrived` / `departed`, with product and EPC. |
| `button.pantry_hub_resync` | Compare and report. Resync never changes Grocy; fix differences there. |

Stock levels come from the Grocy integration (HACS). Pantry Hub doesn't
duplicate them.

## Web UI

Open **Pantry Hub** in the sidebar. It shows status, links (unlink, or
relink by barcode or by Grocy product id written as `#12`, optionally "already
in stock"), unlinked tags,
barcodes that need a Grocy product, failed operations, and the last resync.
It's only reachable through Home Assistant ingress.

## Troubleshooting

- **Reader offline, `dropped_sources` rising**: datagrams arrive from an IP
  other than `allowed_reader_ip`. Check the reader's IP, or NAT between the
  VLANs.
- **Reader offline, counters at 0**: nothing arrives. Check the reader's
  UDP destination (`192.168.1.38:8888`), the firewall rule from the IoT VLAN
  to that port, and the Network port mapping.
- **`bad_frames` rising**: the reader sends something `aiochainway` doesn't
  parse. Set `log_level: debug` and open an issue with the hex.
- **Grocy queue growing**: Grocy is down or the API key changed. See
  `last_error` on `sensor.pantry_hub_grocy_queue`.

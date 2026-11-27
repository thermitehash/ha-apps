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
| `reader_tcp_port` | `8888` | The UR4's TCP command port. |
| `reading_mode` | `door` | **door**: the hub drives the reader in command mode over TCP, from the door sensor (see below). **continuous**: the reader's own auto mode pushes every read over UDP (the 0.1/0.2 behaviour); a fallback. The reader's trigger mode is never used. |
| `reader_connection` | `per_window` | **per_window**: connect for each sweep or continuous window (retrying the connect up to 3 times) and disconnect afterwards; a continuous window keeps its one connection for its whole length. **persistent**: keep one connection open, pinged every 60 s and reconnected automatically. `per_window` is the default until it's known whether the UR4 keeps idle TCP sessions open. |
| `reader_antennas` | `[1]` | UR4 antenna ports to enable (1–4). Bench: `[1]`. Final install with two Vulcan SP11s: `[1, 2]`. **Every enabled port must have an antenna connected.** Door mode applies and verifies this before any inventory (see below). |
| `reader_power_dbm` | `27` | Power for each enabled antenna, 5–30 dBm. Must not be above `max_reader_power_dbm`, or the app refuses to start. |
| `door_entity` | (empty) | The pantry door contact in Home Assistant, a `binary_sensor` (`on` = open). Optional: leave it empty and pick the sensor in the web UI's **Settings** page instead (see below). If set here, it takes precedence over the web UI. A typo that isn't a `binary_sensor.…` id is refused when you save the configuration. |
| `open_continuous_s` | `600` | After the door opens, read continuously this long. |
| `open_sweep_interval_s` | `300` | Then, while the door stays open, sweep this often. |
| `sweep_s` | `45` | Length of one sweep. |
| `closed_sweep_interval_s` | `3600` | While the door is closed (or the sensor is unavailable), sweep this often. |
| `depart_after_missed_sweeps` | `2` | A present item departs after being missing from this many *completed* sweeps in a row. |
| `allowed_reader_ip` | (reader IP) | Only UDP datagrams from this IP are accepted. UDP is unauthenticated, so keep this set. |
| `reader_tcp_probe` | `false` | Also ask the reader for its version over TCP every minute, so it shows online when no tags are in range. |
| `reader_offline_after_s` | `120` | No datagram for this long → reader offline. |
| `grocy_url` | `http://a0d7b954-grocy` | See above. |
| `grocy_api_key` | — | Grocy API key. |
| `grocy_location_id` | `1` | ID of the Pantry location. Arrivals are added there. |
| `grocy_ui_url` | (empty) | Base URL your browser uses for Grocy, so links in the web UI work. If empty, links use `grocy_url`, which only works inside Home Assistant's network. |
| `arrive_reads` / `arrive_window_s` | `3` / `10` | A tag must be read this many times within this window before it counts as arrived. This filters out tags carried past the door. |
| `depart_after_s` | `900` | Not read for this much **reading time** → departed (one unit consumed). The timer only runs while the reader reads continuously (door open, or `continuous` mode); idle time never counts. |
| `depart_requires_door_closed` | `false` | `continuous` mode only: count timer departures only while the door sensor (`door_entity` or the web UI choice) reports closed (for one arrival window). |
| `max_reader_power_dbm` | `27` | Ceiling for `reader_power_dbm`. Values over 27 are refused: with 8.3 dBi antennas (Vulcan SP11) that's the FCC 36 dBm EIRP limit. Use the same ceiling when provisioning the reader with `aiochainway`. |
| `log_level` | `info` | `debug` logs every UDP datagram as hex. |

### Ports

- `reading_mode: door` talks to the reader over **TCP 8888** (outgoing from
  Home Assistant to the reader). No incoming port is needed.
- `reading_mode: continuous`: the reader sends its reads to UDP **8888** on
  the Home Assistant host (change the host port under **Network**).
- UDP **1111**: the reader's own announcements. Pantry Hub listens for 5 s
  at start to learn the reader's MAC for the device id. Announcements are
  broadcasts, so they only arrive if the reader and Home Assistant share a
  network segment; otherwise the id is `host:port`, which works just as
  well.

## Choosing the door sensor

App options can't show an entity picker, so the web UI has one: open
**Pantry Hub** in the sidebar → **Settings**. The dropdown lists every
`binary_sensor` in Home Assistant with its friendly name, entity id and
current state: door, garage door, opening and window sensors first, then the
rest. **Save** stores the choice in Pantry Hub's database (it survives
restarts and updates).

Which one is used:

1. `door_entity` in the app's configuration, if set;
2. otherwise the Settings page choice.

The Settings page says which source is active (and the
`sensor.pantry_hub_reader_activity` attributes `door_entity` /
`door_entity_source` show it too).

**No door sensor from either source** (door mode): Pantry Hub starts anyway
in the **needs door sensor** state. It sweeps every
`closed_sweep_interval_s` (hourly by default), shows a banner in the web UI,
and raises a persistent notification in Home Assistant that links to the
web UI. Both go away as soon as a sensor is chosen.

**Inverted sensor** (reports `on` when the door is closed): fix it at the
device first if you can. Wire the reed switch the other way (NC instead of
NO, or the reverse), or set the sensor's own invert parameter (many Zigbee
and Z-Wave contacts have one). The **Inverted** checkbox next to the picker
is for sensors that can't be changed. The Settings page shows a live line
such as `Door: open (raw: on)`: open the door and check that it says
**open**. If it was already right before you ticked the box, untick it
(inverting twice makes it wrong again).

**Saved sensor not found** (renamed or deleted in Home Assistant): it stays
selected and is marked "not found" on the Settings page. After 2 minutes a
notification says "Saved door sensor … not found in Home Assistant (renamed
or deleted?)". Until then and while it lasts, Pantry Hub uses the
closed-door schedule. Pick the sensor again to fix it.

An unreachable reader at start isn't fatal either: the app starts, shows
the reader offline, and retries each sweep.

## Antennas and power

In door mode Pantry Hub enforces `reader_antennas` and `reader_power_dbm` on
the reader itself. On every new connection to the reader (each window, or
after a reconnect with `reader_connection: persistent`), before any
inventory, it:

1. reads the enabled antenna ports and each port's power;
2. if they differ from the options, sets them (the antenna setting is saved
   on the reader; power is set per enabled port);
3. reads them back and checks they match.

A reader that already matches gets no writes. If a setting can't be applied
or doesn't read back correctly, **no inventory runs**,
`binary_sensor.pantry_hub_reader_online` turns off, and Home Assistant shows
a "Reader antenna settings could not be applied" notification. Pantry Hub
retries on its normal schedule (every 60 s) and dismisses the notification
once the settings verify. `sensor.pantry_hub_reader_activity` shows
`antennas_enabled`, `reader_power_dbm`, `config_applied` (when they last
verified) and `last_config_error`.

- **Bench setup**: `reader_antennas: [1]`, one antenna on port 1.
- **Final install**: `reader_antennas: [1, 2]` for the two SP11s on ports 1
  and 2.
- **Every enabled port must have an antenna connected.** Transmitting into an
  open port can damage the reader, and it wastes read time on a port that
  can't see anything.

### Dead port warning

The UR4 can't tell whether an antenna is plugged in, so Pantry Hub watches
how many reads each enabled port gets in completed sweeps (aborted sweeps
don't count):

- **Two or more ports**: a port with no reads in each of the last 5 sweeps,
  while the other ports read at least 10 times in total over those sweeps.
- **One port**: no reads in 5 sweeps while tags were in the pantry when the
  silence started.

Home Assistant then shows "Antenna on port N gave no reads in 5 sweeps —
check its cable and connection." It clears as soon as that port reads again
(in any window, including continuous reading). It's a warning only: reading
and presence carry on unchanged. `sensor.pantry_hub_reader_activity` shows
`per_port_reads_last_sweep` and `dead_ports`.

A port can also be quiet for a good reason (nothing within its reach), so
treat the warning as "check the cable", not proof of a fault.

`reading_mode: continuous` leaves the reader alone (it runs in its own auto
mode there); set antennas and power when provisioning it.

## Door-driven reading (default)

```
start ───────────────▶ one sweep
door opens ──────────▶ read continuously for open_continuous_s (600 s),
                       then a sweep_s (45 s) sweep every open_sweep_interval_s (300 s)
door closes ─────────▶ one sweep now, then stop
door closed ─────────▶ a sweep every closed_sweep_interval_s (3600 s)
sensor unavailable ──▶ same as closed (never "open forever"), plus a
                       notification in Home Assistant after 2 minutes
"Sweep now" button ──▶ one sweep now
```

- **Arrival**: 3 reads within 10 s, in any reading window (continuous or
  sweep).
- **Departure**: a present item that is missing from
  `depart_after_missed_sweeps` (2) **completed** sweeps in a row. A sweep
  counts only if the reader stayed online for the whole window and answered
  (with tags, or an empty inventory). A sweep cut short by a reader drop or
  a door event never counts as a miss. While the door is open, the
  `depart_after_s` timer also applies, counting only continuous reading time.
- **Restart**: one sweep at start; nothing departs until 2 sweeps have
  completed since the restart.
- Each window is an aiochainway inventory whose result says how it ended.
  Only a window that ran its full duration counts as a completed sweep. If
  the reader doesn't acknowledge the stop at the end, the sweep still counts
  (the duration elapsed) and the log says so.
- **Between windows** the hub checks the reader every 10 minutes (connect,
  ping, disconnect) so `reader_online` stays accurate. The check is skipped
  if a window ran in those 10 minutes.
- The hub puts the reader in **command mode** itself (it starts and stops
  every inventory over TCP); if the reader was in auto mode it's switched
  and a warning is logged.
- Home Assistant apps can't add items to the **Repairs** dashboard (only
  integrations can), so an unavailable door sensor raises a persistent
  notification instead. It's dismissed automatically when the sensor
  reports again.

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
- **Restart**: tags that were in the pantry are treated as present. In door
  mode nothing departs until 2 sweeps complete; in continuous mode, until one
  full `depart_after_s` window has passed. A restart never consumes
  everything at once.

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
you started)? Link it with **already in stock**: at the linking station,
scan the printed **mode card** (`docs/mode-card-in-stock.pdf`) in link mode
(the pulse turns amber; every link in that session is in-stock), or tick the
checkbox in the web UI's Link form. Over MQTT this is `"in_stock": true` in
the station's barcode or tag message. The add for its current arrival (or the next one, if the tag
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
| `binary_sensor.pantry_hub_reader_online` | Door mode: the last window reached the reader. Continuous mode: a datagram within `reader_offline_after_s` (or a TCP probe answer). Attributes: datagram/tag/bad-frame/dropped-source counters. |
| `sensor.pantry_hub_present_tags` | Tags in the pantry (present or briefly missing). |
| `sensor.pantry_hub_unlinked_tags` | Unlinked tags currently in the pantry. |
| `sensor.pantry_hub_grocy_queue` | Grocy changes waiting. Attributes: `failed` (includes review), `review`, `online`, `last_error`. |
| `sensor.pantry_hub_last_link` | Product of the most recent link; attributes: EPC, barcode, station, time. |
| `sensor.pantry_hub_resync_differences` | Products whose Grocy pantry amount differs from the linked tags present, after the last resync. |
| `event.pantry_hub_item` | `arrived` / `departed`, with product and EPC. |
| `sensor.pantry_hub_reader_activity` | `idle` / `continuous` / `sweep`. Attributes: reading mode, door state, sweeps completed/aborted, last sweep and last attempt, reader id, reader connection mode, whether departures are allowed yet, whether the door notification is up. |
| `sensor.pantry_hub_last_sweep` | Time of the last completed sweep. |
| `button.pantry_hub_sweep_now` | Sweep now (door mode). During a continuous window it sweeps, then continuous reading resumes. |
| `button.pantry_hub_resync` | Compare and report. Resync never changes Grocy; fix differences there. |

Stock levels come from the Grocy integration (HACS). Pantry Hub doesn't
duplicate them.

## Web UI

Open **Pantry Hub** in the sidebar. It shows status, links (unlink, or
relink by barcode or by Grocy product id written as `#12`, optionally "already
in stock"), unlinked tags,
barcodes that need a Grocy product, failed operations, and the last resync.
**Settings** has the door sensor picker. It's only reachable through Home
Assistant ingress.

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

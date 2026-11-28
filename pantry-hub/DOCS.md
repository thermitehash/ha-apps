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
**Pantry Hub** in the sidebar → **Settings**. Type to search; the list shows
every `binary_sensor` in Home Assistant with its friendly name and, small
underneath, its entity id: door, garage door, opening and window sensors
first, then the rest. Tapping one saves it in Pantry Hub's database (it
survives restarts and updates). Below the list, a large **Door: OPEN** /
**Door: CLOSED** shows what Pantry Hub sees, updated every second.

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

**Is it the right way round?** Close the pantry door, tap **Check it**, then
open the door. After a few seconds Pantry Hub compares what the sensor says:
"Looks right ✓", or "This sensor reports the opposite. Flip it?" with a
**Flip** button. Flipping is stored in Pantry Hub (the same setting as the
0.3.x "Inverted" checkbox). If you can, fix a reversed sensor at the device
instead: wire the reed switch the other way (NC instead of NO, or the
reverse), or set the sensor's own invert parameter (many Zigbee and Z-Wave
contacts have one). **Advanced → Reverse open/closed** is the same setting
as a plain switch.

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

### Tagging a Kroger delivery (Just bought)

With the **Kroger Restock** integration installed, delivered items show on
the Pantry tab under **Just bought** ("Just bought · 6 items to tag"), each
with its picture, size and "×3 of 3 to tag". For each one:

- **Tag them**: the linking station Pantry Hub heard from last (default
  `kroger_scanner`) pulses blue. Tap one tag at a time on it; the page counts
  "2 of 3 linked". Tags already linked to that product are skipped. It stops
  at the count, or 60 s after the last tag (tap **Tag them** again for the
  rest). The tagged items reach Grocy stock when they go into the pantry,
  like any linked tag.
- **Not tagging**: removes the row. With **Add to Grocy stock** ticked (the
  default) the untagged units are booked in Grocy as a purchase, with the
  price Kroger reported. If you tag one of them within 7 days anyway, its
  arrival is matched against that booking (listed in recent events) and not
  added again.

Deliveries are matched to Grocy by the barcode printed on the package
(Kroger Restock's `barcode`, e.g. `049000028911`); Kroger's own 13-digit
code without the check digit (`0004900002891`) and the EAN-13 form are tried
too, so a product stored either way is found.
If Grocy doesn't know the barcode, Pantry Hub creates the product from
Kroger's name, brand and size (and picture). Rows older than 14 days move
under **Older** and are deleted after 30 days; nothing is ever booked by
itself. The card is hidden when Kroger Restock isn't installed. The linking
station needs firmware 0.3.0 or later.

### Unloading groceries from elsewhere (Unload)

For anything Kroger Restock doesn't report (the Kroger app, in-store, other
shops). On the linking station **triple-click** the button: the LED pulses
slowly green. Or tap **Start unloading** on the Pantry page. Then scan each
item; scanning the same product again adds 1. On a phone, **Scan barcode**
opens the camera (or type the barcode).

For each barcode Pantry Hub finds the Grocy product (printed code, UPC-A /
EAN-13 and Kroger's 13-digit form all match). If Grocy doesn't have it, it
creates it from Kroger Restock's lookup (name, brand, size, picture), then
from Grocy's barcode lookup plugin; if neither knows it, the barcode goes to
"Barcodes without a Grocy product". Unloaded items appear in **Just bought**
and work like Kroger deliveries: **Tag them**, or **Not tagging** (booked in
Grocy without a price). Nothing is ever added to the Kroger cart. Unload
ends after 3 minutes without a scan, with a single click on the station, or
**Stop unloading**.

With Kroger Restock 0.17.3 or later, a pickup with items it couldn't match
(bought in the Kroger app or substituted, not on its list) shows "About 12
more items from your Kroger order — start unloading" on the Just bought card,
with the order's item count from Kroger Restock's "untracked items in last
order" sensor. It counts down as you unload within 6 hours of pickup (it's
"about": Kroger Restock reports a net difference, so it stops at 0 and
further scans still count as unloaded). Scanning something Kroger Restock
already tracked in that order doesn't count down. A pickup while Pantry Hub
was off still shows the prompt when it starts. Older Kroger Restock
versions show nothing.

The phone camera needs Home Assistant opened over HTTPS (or on the device
itself); otherwise type the barcode. Browsers without a built-in barcode
reader use a copy of ZXing that comes with Pantry Hub (no internet needed).

### Stock-take (what's already in the pantry)

For the first setup and any later recount. **Tags → Start stock-take**, or
hold the station button for **6 seconds** (slow amber pulse). Scan each
**untagged** unit once, or scan a product once and type how many; tagged
items are counted by the reader. **Finish stock-take** shows the review: per
product the counted units (plus tags the reader sees), Grocy's amount in the
pantry, and the difference. Then:

- **Set Grocy to the counts**: an inventory correction per product (Grocy's
  inventory, not a purchase), so prices and history aren't faked. Units of
  that product in other locations are kept.
- **Add missing items as purchases**: only positive differences are added
  (no price); surplus in Grocy is left alone.

Products you didn't scan are left alone unless **Products I didn't scan are
gone** is ticked (then their pantry amount is set to what the tags show,
usually 0). Products whose every unit is tagged and agree with Grocy don't
appear: the reader owns them.

After applying, each counted product has **Tag N**: tap it, then tap each
item on the station. Those items' first reader arrival doesn't add them
again (like Not tagging, for 7 days).

### In Grocy, not tagged

The Tags page lists products Grocy has in the pantry location with more
units than tags (for example stock you entered in Grocy by hand), with
**Tag them**. Those links are "already in stock", so tagging never adds them
to Grocy again.

### Product pictures

When a barcode is linked and its Grocy product has no picture, Pantry Hub
looks the barcode up in Kroger Restock (`kroger_restock.lookup_product` with
`upc` set to the barcode; only the barcode leaves your network), downloads the image (at most 5 MB, 10 s),
shrinks it to 600 px and saves it in Grocy as `pantryhub_<product id>.jpg`.
An empty product name, or one that's just the barcode, and an empty
description ("Brand · size") are filled in; names you typed are kept. It runs
in the background and retries a few times, so linking is never slowed down.

Pantry Hub only changes products whose picture is empty or one it set itself
(`pantryhub_…`); pictures you chose in Grocy stay. **Settings → Product
pictures → Fetch missing pictures** (or `button.pantry_hub_fetch_pictures`)
goes through every product Pantry Hub has linked, one lookup every 2 seconds,
showing progress like "12 of 40, 3 not found". A product Kroger has no
picture for isn't asked about again for 30 days.

Without Kroger Restock nothing is looked up, and Details shows "Product
pictures: Kroger Restock not found." If Kroger Restock reports that its
Kroger sign-in failed, lookups stop and Details says "Kroger Restock needs
you to sign in again": sign in again in Kroger Restock, then tap **Fetch
missing pictures**. If Kroger rate-limits the lookups, Pantry Hub waits an
hour before asking again.

### MQTT topics

| Topic | Direction | Payload |
|---|---|---|
| `pantry_hub/linking/<station>/barcode` | station → hub | `{"barcode": "...", "ts": ..., "in_stock"?: true}` |
| `pantry_hub/linking/<station>/tag` | station → hub | `{"epc": "...", "rssi": -45.2, "ts": ..., "in_stock"?: true}` |
| `pantry_hub/linking/<station>/result` | hub → station | `{"status": "linked\|unknown_barcode\|tag_already_linked\|timeout\|error", "product": "...", "message": "..."}`; in a counted session also `"count", "linked", "remaining"` |
| `pantry_hub/linking/<station>/start` | hub → station | `{"barcode": "...", "count": 3}`: start a counted session (Tag them) |
| `pantry_hub/linking/<station>/scan` | station → hub | `{"barcode": "...", "mode": "unload\|stocktake", "ts": ...}` (answer on `result`: `added`, `counted`, `unknown_barcode`, `error`) |
| `pantry_hub/linking/<station>/mode_state` | station → hub | `{"mode": "kroger\|link\|unload\|stocktake"}` after a gesture or timeout |
| `pantry_hub/linking/<station>/mode` | hub → station | `{"mode": "unload\|stocktake\|off"}` (web UI started / stopped it) |
| `pantry_hub/pictures/press` | anyone → hub | fetch missing pictures |
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
| `button.pantry_hub_fetch_pictures` | Fetch missing product pictures from Kroger Restock. |

Stock levels come from the Grocy integration (HACS). Pantry Hub doesn't
duplicate them.

## Web UI

Open **Pantry Hub** in the sidebar. It updates itself (every 3 seconds while
you're looking at it, and straight away when you come back to it), so there's
no need to pull down to refresh. "Updated 3 s ago" under the title shows how
fresh it is; if Pantry Hub stops answering, a "Can't reach Pantry Hub —
retrying" banner appears. Three tabs (at the bottom on a phone, at the top on
a wide screen):

- **Pantry**: one line that says whether everything is fine or names the most
  important problem and how to fix it (tap it for **Details**: reader, Grocy,
  MQTT, sweep counters, reads per antenna port). Then the door and when the
  pantry was last checked (**Check now** checks immediately), and what's in
  the pantry, grouped by Grocy product with a count and "since Tue". Product
  pictures come from Grocy when it has one. With Kroger Restock, **Just
  bought** above it lists deliveries to tag.
- **Tags**: tags that aren't linked yet (**How to link** shows the linking
  station steps, and a field to link by barcode or Grocy product id written as
  `#12`, optionally "already in stock"; **Forget**), linked tags (search; the
  **⋯** menu has **Unlink** and **Relink**), failed Grocy updates (**Retry**,
  or **Consume now** / **Dismiss** for one waiting for review), and barcodes
  that need a Grocy product.
- **Settings**: the door sensor picker and check (above), the reader options
  that matter (change them in the app's Configuration tab), **Compare
  with Grocy** (the resync report) and **Fetch missing pictures**.

It follows the phone's light or dark mode, and is only reachable through Home
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

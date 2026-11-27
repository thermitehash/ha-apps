# Pantry Hub setup

Target: Home Assistant OS 18.4 · Core 2026.11.2 · Supervisor 2026.11.0 ·
ODROID-N2 (`aarch64`) at `192.168.1.38`.

Work through the steps in order. Each one says how to check it worked.

## 1. Free disk space first

The HA disk is **86% full**. Pantry Hub's image is small (Python on Alpine,
roughly 100 MB), but Grocy, Mosquitto and the backups you'll take before
updating all need room. Get below ~75% before installing anything:

- **Settings → System → Repairs → ⋮ → System information** shows disk use.
- **Settings → System → Backups**: delete old backups, or move them off the
  box (a backup location such as a NAS or Google Drive).
- **Settings → System → Storage**: check the database size. If
  `home-assistant_v2.db` is large, lower `recorder: purge_keep_days` or
  exclude chatty entities, then run `recorder.purge` with `repack: true`.
- Remove apps you no longer use (**Settings → Apps**).
- Clear old media/downloads under `/media` and `/share`.

Check: disk use below ~75%.

## 2. MQTT

1. **Settings → Apps → App store → Mosquitto broker → Install → Start**
   (Start on boot on, Watchdog on).
2. **Settings → Devices & services**: Home Assistant discovers the MQTT
   integration. Click **Configure**.
3. For the linking station, create a Home Assistant user for MQTT (e.g.
   `linking-station`, "Can only log in from the local network" on). The
   Mosquitto app accepts Home Assistant users.

Pantry Hub doesn't need an MQTT user: it gets credentials from the
Supervisor (`services: mqtt:need`).

Check: the MQTT integration shows as configured.

## 3. Grocy

1. Add the community app repository if it isn't there yet:
   **Settings → Apps → App store → ⋮ → Repositories** →
   `https://github.com/hassio-addons/repository`.
2. Install **Grocy**. In its **Configuration** tab:
   - turn **ssl** off (unless you have certificates set up),
   - under **Network**, enter a port for `80/tcp` (any free port, e.g.
     `9192`). This enables Grocy's direct HTTP server, which Pantry Hub
     reaches internally at `http://a0d7b954-grocy`. Without a port, Grocy
     only answers ingress, and Pantry Hub can't reach it.
3. Start Grocy and open it from the sidebar.
4. **Create the Pantry location**: wrench → *Manage master data* →
   *Locations* → **Add**: name `Pantry`. Open it again: the URL ends in
   `/location/<id>`. That id is `grocy_location_id`.
5. **Create an API key**: wrench → *Manage API keys* → **Add**. Copy the
   key. That's `grocy_api_key`. (Put it only in the app options; never in
   git.)
6. **Optional, the barcode lookup plugin** (creates products from
   OpenFoodFacts when you scan an unknown barcode at the linking station):
   Grocy uses the OpenFoodFacts plugin by default. If your Grocy config
   sets `STOCK_BARCODE_LOOKUP_PLUGIN` to something else, set it back to
   `OpenFoodFactsBarcodeLookupPlugin`. Without a working plugin, unknown
   barcodes go to the hub's "needs product" list instead.
7. Set **minimum stock amounts** on products you want restocked (product →
   edit → *Minimum stock amount*).

**Grocy HACS integration** (stock entities in HA): HACS → Integrations →
*Grocy* → Download, restart HA, then **Settings → Devices & services → Add →
Grocy** with URL `http://a0d7b954-grocy`, the port you chose, and the API
key (a separate key for the integration is tidier). Turn on the entities you
want; `binary_sensor.grocy_missing_products` is the one the restock
automation uses.

Check: `curl -H "GROCY-API-KEY: …" http://<ha-ip>:9192/api/system/info`
from your Mac returns JSON with `grocy_version`.

## 4. Add this app repository

[![Add repository](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fthermitehash%2Fha-apps)

or **Settings → Apps → App store → ⋮ → Repositories** →
`https://github.com/thermitehash/ha-apps`.

## 5. Configure and start Pantry Hub

Install **Pantry Hub**. The image is prebuilt
(`ghcr.io/thermitehash/pantry-hub`), so the N2 only downloads it.

Options (the rest can stay at their defaults):

```yaml
reader_host: 192.168.99.202     # the UR4's static IP (example; use yours)
allowed_reader_ip: ""           # empty = same as reader_host
grocy_url: http://a0d7b954-grocy
grocy_api_key: "<from step 3>"
grocy_location_id: <id from step 3>
grocy_ui_url: ""                # optional, e.g. http://192.168.1.38:9192
max_reader_power_dbm: 27
reader_antennas: [1]            # bench; [1, 2] once both SP11s are mounted
reader_power_dbm: 27            # ≤ max_reader_power_dbm
```

Every port in `reader_antennas` must have an antenna connected. In door mode
Pantry Hub sets and verifies these on the reader before it reads anything.

**Network**: leave `8888/udp` → `8888`.

Start it and open the **Log** tab. You want to see:

```
Grocy 4.x OK; pantry location is 'Pantry' (id N)
Listening for UR4 reads on UDP 0.0.0.0:8888 (accepting 192.168.99.202)
MQTT connected to core-mosquitto:1883
Pantry Hub running
```

If Grocy is wrong, the log says why ("refused the API key", "location … does
not exist", "not reachable") and the app stops.

Check: **Settings → Devices & services → MQTT → Pantry Hub** shows the
entities. `binary_sensor.pantry_hub_reader_online` stays off until the
reader is set up.

## 6. Provision the UR4 (from your Mac, with `aiochainway`)

Do this once, with the reader on your desk or in place. See `aiochainway`'s
`docs/bringup-macos.md` for the full walkthrough.

```sh
python3.14 -m venv ~/.venvs/ur4 && . ~/.venvs/ur4/bin/activate
pip install "aiochainway @ git+https://github.com/thermitehash/aiochainway@v0.2.0"
aiochainway --help
```

1. **Static IP on the IoT VLAN**: set it with the `aiochainway` CLI as
   described in `bringup-macos.md` (the reader's IP is changed once from the
   CLI, never from HA). Reserve that IP in your DHCP server too.
2. **Provision**: region USA, antennas you've connected, **power ≤ 27 dBm**,
   work mode **command** (Pantry Hub's default `reading_mode: door` starts
   and stops every inventory itself over TCP; it also switches the reader to
   command mode at start if needed). Don't use the reader's trigger mode.
   The `provision` helper (Python) does it in one go:

   ```python
   import asyncio
   from aiochainway import provision, FrequencyRegion, WorkMode

   asyncio.run(provision(
       "192.168.99.202",
       region=FrequencyRegion.USA,
       antennas=[1],          # bench; [1, 2] for the two SP11s. Only ports with an antenna!
       power=25,              # dBm; never above 27 with the 8.3 dBi SP11s
       work_mode=WorkMode.COMMAND,
       confirm=True,
   ))
   ```

   Fallback (`reading_mode: continuous`): `work_mode=WorkMode.AUTO` plus
   `udp_dest=("192.168.1.38", 8888)`.

   The 27 dBm ceiling: Vulcan SP11 antennas are 8.3 dBi, and FCC Part 15
   allows 36 dBm EIRP → 36 − 8.3 = 27.7 dBm conducted, minus nothing for
   cable loss to be safe.
3. **Firewall** (door mode): allow **TCP from `192.168.1.38` to the reader's
   port 8888** (HA → IoT VLAN), with return traffic. Continuous mode instead
   needs **UDP from the reader to `192.168.1.38:8888`**.
4. **Door sensor**: open Pantry Hub in the sidebar → **Settings** and pick
   the pantry door contact (a `binary_sensor`, `on` = open), or set it as
   `door_entity` in the app's configuration (that wins if both are set).
   Until one is chosen, the app sweeps hourly and shows a "needs door
   sensor" notification.

Check: the log shows `Sweep (startup)` then `Sweep done`;
`sensor.pantry_hub_last_sweep` gets a time and
`binary_sensor.pantry_hub_reader_online` turns on. Open the door:
`sensor.pantry_hub_reader_activity` shows `continuous`, and tags in range
count up in `sensor.pantry_hub_present_tags`. (Continuous mode: if
`dropped_sources` rises, set `allowed_reader_ip` to the address the log
shows.)

## 7. Linking station

Flash `esphome/linking-station/` onto the Kroger wall scanner (see its
README): same device, same API key, plus the U107 and MQTT credentials from
step 2. **Use a 5 V ≥ 2 A USB-C supply.**

## 8. Automations

See [automations.md](automations.md) for low stock → Kroger Restock.

## Ports, firewall and power summary

| What | Where | Rule |
|---|---|---|
| Pantry Hub → UR4 (door mode, default) | TCP 8888 on the reader | allow from 192.168.1.38 (HA → IoT VLAN) |
| UR4 → Pantry Hub (continuous mode) | UDP 8888 on 192.168.1.38 | allow from the reader's IP (IoT VLAN) |
| UR4 announcements (optional) | UDP 1111 broadcast | only arrive if reader and HA share a segment; for the MAC device id |
| Linking station → Mosquitto | TCP 1883 on 192.168.1.38 | allow from the station's IP |
| Linking station ↔ HA API | TCP 6053 on the station | as today |
| Grocy direct port | e.g. TCP 9192 | LAN only; needed so other apps can reach Grocy |
| Reader power | ≤ 27 dBm conducted | 8.3 dBi antennas, FCC 36 dBm EIRP |
| Station power | 5 V ≥ 2 A USB-C | QRCode2 + U107 |

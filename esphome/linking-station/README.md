# Linking station firmware

This turns the Kroger Restock wall scanner into a barcode + RFID linking
station for [Pantry Hub](../../pantry-hub/). It's the same AtomS3 Lite +
QRCode2 base, plus an M5Stack Unit UHF-RFID (U107) on the Grove port. It
keeps the device name (`kroger-scanner`), the HA feedback action
(`esphome.kroger_scanner_feedback`) and everything Kroger mode does today,
so the existing Kroger feedback automation keeps working unchanged.

Config: [`kroger-scanner.yaml`](kroger-scanner.yaml) · U107 driver:
[`components/jrd4035/`](components/jrd4035/)

## Hardware

| Part | Notes |
|---|---|
| M5Stack **AtomS3 Lite** | RGB LED GPIO35, button GPIO41 |
| M5Stack **Atomic QRCode2 Base** (A133-B) | M14 scan engine, on the Atom's bottom header |
| M5Stack **Unit UHF-RFID** (U107) | JRD-4035 module, Grove cable to the Atom's Grove port |
| **5 V ≥ 2 A USB-C supply** | The QRCode2 base and the U107 together draw well over what the wall scanner's 1 A supply was sized for (the U107 alone can draw several hundred mA at full TX power). Use a 2 A or bigger supply and a short, good cable. |

**Housing:** the existing wall housing (`ha-kroger-restock`
`hardware/housing/`) doesn't fit the U107. Mount the U107 beside the scanner,
**antenna facing outward, away from where tagged items are stored**. Housing
changes will come in a separate `ha-kroger-restock` PR.

## Pins and UARTs

| Signal | AtomS3 pin | Notes |
|---|---|---|
| QRCode2 TX → Atom RX | GPIO5 | `scanner_rx_pin`, UART 115200 |
| Atom TX → QRCode2 RX | GPIO6 | `scanner_tx_pin` |
| QRCode2 TRIG | GPIO7 | pulse-trigger mode only; unused (auto mode) |
| QRCode2 LED | GPIO8 | illumination, driven by the M14; unused |
| U107 TX → Atom RX | GPIO1 (Grove) | `u107_rx_pin`, UART 115200 |
| Atom TX → U107 RX | GPIO2 (Grove) | `u107_tx_pin`. **If no tag is ever read, swap GPIO1/GPIO2.** |
| U107 power | Grove 5 V | from USB-C |
| RGB LED | GPIO35 | |
| Button | GPIO41 | active low |

The ESP32-S3 has three hardware UARTs: one goes to the scanner and one to
the U107. Logs go over USB-CDC (`hardware_uart: USB_SERIAL_JTAG`).

## Modes

| | Kroger mode (default) | Link mode |
|---|---|---|
| Enter / leave | the default; **double-click** the button to enter link mode | **double-click** again, or 60 s idle, returns to Kroger mode |
| LED when idle | off (unchanged) | slow, dim **blue pulse** |
| Barcode | `kroger_restock.scan_upc` with `source: esphome`, exactly as before | published to `pantry_hub/linking/kroger_scanner/barcode`; the U107 then polls for up to 15 s and publishes the strongest tag to `.../tag` |
| Result | HA feedback automation: green in cart, amber waiting, blue needs review, red error | hub result on `.../result`: **green** linked, **amber** unknown barcode, **magenta** tag already linked, **red** error/timeout |
| Short press | print list (after ~350 ms, to tell it from a double-click) | same |
| Hold 1–6 s | sync cart | same |

Leaving link mode (by double-click or after 60 s idle) shows one short cyan
flash, then the LED goes off.

### In-stock session (mode card)

For tagging stock Grocy already counts. Print
[`docs/mode-card-in-stock.pdf`](../../docs/mode-card-in-stock.pdf) (or the
PNG) at 100%: a credit-card-sized card with the QR code
`PANTRY-HUB:IN-STOCK`.

1. Double-click: link mode (dim **blue** pulse).
2. Scan the card: the pulse turns dim **amber**. That's the in-stock session.
3. Link items as usual. Every link is published with `"in_stock": true`,
   so Pantry Hub skips the Grocy add for that item's current (or next)
   arrival. When it leaves the pantry, one unit is still consumed.
4. End the session by scanning the card again (back to blue), by
   double-clicking (leaves link mode), or by 60 s of inactivity.

The card does nothing in Kroger mode (amber double blink, like any other
QR code). The **In-stock session** binary sensor shows the state in Home
Assistant. Mode logic lives in [`include/link_mode.h`](include/link_mode.h)
and is unit-tested on the host.

### U107 radio

- TX power defaults to **15 dBm** (`u107_tx_power_dbm`), low so only the item
  in your hand is read. The U107 is specified up to 26 dBm. The firmware
  logs a warning if the module rejects a value; check the first-run
  checklist.
- The radio transmits only during a link scan. Outside a scan the module is
  sent to sleep (`0x17`) and nothing polls it, so in normal Kroger use the
  wall scanner never reads tagged items nearby. The Grove port still
  supplies 5 V to the module; cutting that would need extra hardware.
- After the first tag it keeps polling for 1 s and sends only the strongest
  tag, which avoids linking a neighbouring item. The hub applies the same
  1 s rule.

### Beep (unverified, off by default)

The QRCode2's buzzer belongs to the M14 scan engine. Its documented UART
command only switches "beep on successful decode" (`21 63 42 01`), which
already confirms every barcode read. The **Link result beep (unverified)**
switch (a config entity, off by default) sends `m14_beep_cmd` 1/2/3 times
after a link result. With the default bytes this may make no sound. If you
find a direct buzzer command in M5Stack's M14 manual, put it in
`m14_beep_cmd`. The LED is the primary feedback either way.

## Install

1. Copy `kroger-scanner.yaml`, the `components/` folder and the `include/`
   folder into your ESPHome config folder (keep them side by side).
2. `secrets.yaml`: copy [`secrets.yaml.example`](secrets.yaml.example) and
   fill it in. **Keep the wall scanner's existing `kroger_scanner_api_key`**
   so Home Assistant stays paired. Add an MQTT user (a Home Assistant user,
   e.g. `linking-station`) for the Mosquitto broker.
3. Flash it (OTA from the ESPHome dashboard works, since it's the same
   device).
4. In Home Assistant, the ESPHome device must still have **Allow the device
   to perform Home Assistant actions** on.
5. Double-click the button: the LED should pulse blue. Scan a product, hold
   its tag near the U107, and watch for green.

MQTT is used only for the linking topics. MQTT discovery is off, so the
device's entities come from the native API as before (new: **Link mode**,
**In-stock session**, **Last linked tag**, **Link result beep (unverified)**).

## Validate and test locally

```sh
pip install esphome
cp secrets.yaml.example secrets.yaml   # dummy values are fine for validation
esphome config kroger-scanner.yaml
g++ -std=c++17 -Wall -Wextra -Werror -Icomponents/jrd4035 tests/test_jrd4035_protocol.cpp -o /tmp/t && /tmp/t
g++ -std=c++17 -Wall -Wextra -Werror -Iinclude tests/test_link_mode.cpp -o /tmp/l && /tmp/l
```

The protocol code (`jrd4035_protocol.h`) has no ESPHome dependencies and is
unit-tested on the host. The frame format and command bytes come from
M5Stack's published U107 / JRD-4035 command set. Nothing here has run on
real hardware yet.

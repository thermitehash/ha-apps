# ha-apps

Home Assistant apps by thermitehash.

[![Open your Home Assistant instance and show the add app repository dialog with this repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fthermitehash%2Fha-apps)

Or add it by hand: **Settings → Apps → App store → ⋮ → Repositories** and
paste `https://github.com/thermitehash/ha-apps`.

## Apps

| App | What it does |
|---|---|
| [Pantry Hub](pantry-hub/) | RFID pantry inventory: Chainway UR4 reads become Grocy stock, with a barcode + RFID linking station. |

Images are prebuilt for `aarch64` and `amd64` and pulled from GHCR
(`ghcr.io/thermitehash/pantry-hub`), so nothing is compiled on your Home
Assistant host.

## Also in this repo

- [`esphome/linking-station/`](esphome/linking-station/): ESPHome firmware
  for the barcode + RFID linking station (the Kroger Restock wall scanner
  with an M5Stack UHF RFID unit added).
- [`docs/`](docs/): [setup](docs/setup.md), [automations](docs/automations.md),
  [tagging](docs/tagging.md).

## Development (macOS or Linux)

```sh
cd pantry-hub
python3.14 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff format --check . && ruff check . && mypy
pytest --cov=pantry_hub --cov-branch
```

Run the hub locally against a real or fake Grocy and any MQTT broker:

```sh
PANTRY_HUB_OPTIONS=./options.json PANTRY_HUB_DATA=./data \
PANTRY_HUB_MQTT_HOST=localhost PANTRY_HUB_WEB_ALLOW='*' \
PANTRY_HUB_WEB_PORT=8099 python -m pantry_hub
```

The tests use `aiochainway.testing` (fake UR4), a fake Grocy server and an
in-process MQTT broker (`amqtt`). No hardware is needed.

## Conventions

SemVer; the app version lives only in `pantry-hub/config.yaml`. Every PR
that touches `pantry-hub/` bumps it and adds a `pantry-hub/CHANGELOG.md`
entry (CI checks). Branch `claude/…` → PR titled `vX.Y.Z: summary` →
squash-merge. Pushing a `pantry-hub-vX.Y.Z` tag (or merging a version bump to
`main`) publishes the images.

## License

MIT

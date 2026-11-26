# Automations

Pantry Hub doesn't run automations. Home Assistant does. These examples
use entities from the Grocy HACS integration, Kroger Restock and Pantry Hub.

## Low stock → Kroger Restock

When Grocy says a product is below its minimum stock, add it to the Kroger
Restock list.

- Trigger: `binary_sensor.grocy_missing_products` (Grocy HACS integration).
  Its `missing_products` attribute lists the products below their minimum
  stock amount (each with `id`, `name`, `amount_missing`, …).
- Action: `kroger_restock.add_item` (from `ha-kroger-restock`
  `services.yaml`). Fields: `name` (text), `quantity` (1–99), `source`
  (`scan | shortcut | esphome | manual | assist`), optional `url`,
  `already_in_cart`, `config_entry_id`. Kroger Restock matches the name to a
  Kroger product. Items it can't match go to its "needs review" state.
- If the product has a UPC in Grocy, `kroger_restock.scan_upc` with
  `upc: …` is the more exact alternative (same fields as the wall scanner
  uses, plus `source`). The Grocy integration's attribute doesn't include
  barcodes, so the example below uses names.

Paste into **Settings → Automations → Create automation → ⋮ → Edit in YAML**:

```yaml
alias: Pantry - restock low Grocy products via Kroger
description: >-
  When a product drops below its Grocy minimum stock, add it to the Kroger
  Restock list once (until it is back in stock).
mode: queued
max: 20
triggers:
  - trigger: state
    entity_id: binary_sensor.grocy_missing_products
    attribute: missing_products
variables:
  before: >-
    {{ (trigger.from_state.attributes.missing_products | default([], true))
       | map(attribute='id') | list }}
  newly_missing: >-
    {{ (trigger.to_state.attributes.missing_products | default([], true))
       | rejectattr('id', 'in', before) | list }}
conditions:
  - condition: template
    value_template: "{{ newly_missing | count > 0 }}"
actions:
  - repeat:
      for_each: "{{ newly_missing }}"
      sequence:
        - action: kroger_restock.add_item
          data:
            name: "{{ repeat.item.name }}"
            quantity: "{{ [repeat.item.amount_missing | int(1), 1] | max }}"
            source: manual
```

Only products that *newly* drop below minimum are added, so a restart or a
second product going low doesn't add the first one again. If you keep
Kroger Restock's list in a to-do entity, check there for duplicates the first
few days.

## Notify on unlinked tags in the pantry

```yaml
alias: Pantry - unlinked tag in pantry
mode: single
triggers:
  - trigger: numeric_state
    entity_id: sensor.pantry_hub_unlinked_tags
    above: 0
    for: "00:05:00"
actions:
  - action: notify.notify
    data:
      title: Pantry
      message: >-
        {{ states('sensor.pantry_hub_unlinked_tags') }} unlinked tag(s) in the
        pantry. Link them at the scanner or in the Pantry Hub page.
```

## Item events

`event.pantry_hub_item` fires `arrived` / `departed` with `product` and
`epc` attributes. Other consumers can subscribe to the MQTT topic
`pantry_hub/events` directly.

```yaml
alias: Pantry - log item movements
mode: queued
triggers:
  - trigger: state
    entity_id: event.pantry_hub_item
    not_from: unavailable
actions:
  - action: logbook.log
    data:
      name: Pantry
      message: >-
        {{ trigger.to_state.attributes.event_type }}:
        {{ trigger.to_state.attributes.product or 'unlinked tag' }}
```

## Reader offline

```yaml
alias: Pantry - RFID reader offline
mode: single
triggers:
  - trigger: state
    entity_id: binary_sensor.pantry_hub_reader_online
    to: "off"
    for: "00:10:00"
actions:
  - action: notify.notify
    data:
      message: The pantry RFID reader has sent nothing for 10 minutes.
```

## Linking station feedback

The linking station is the reflashed wall scanner and keeps the name
`kroger-scanner`, so Kroger Restock's existing feedback automation
(`ha-kroger-restock` `esphome/ha_feedback_automation.yaml`, calling
`esphome.kroger_scanner_feedback`) keeps working unchanged. Link-mode
feedback comes straight from Pantry Hub over MQTT, so no automation is
needed for it.

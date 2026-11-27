"""MQTT topics and the Home Assistant device-discovery payload."""

from __future__ import annotations

from typing import Any

PREFIX = "pantry_hub"
AVAILABILITY_TOPIC = f"{PREFIX}/status"
STATE_TOPIC = f"{PREFIX}/state"
EVENTS_TOPIC = f"{PREFIX}/events"
LAST_LINK_TOPIC = f"{PREFIX}/last_link"
RESYNC_COMMAND_TOPIC = f"{PREFIX}/resync/press"
RESYNC_RESULT_TOPIC = f"{PREFIX}/resync/result"
SWEEP_COMMAND_TOPIC = f"{PREFIX}/sweep/press"
LINKING_SUB = f"{PREFIX}/linking/+/+"
HA_STATUS_TOPIC = "homeassistant/status"
DISCOVERY_TOPIC = "homeassistant/device/pantry_hub/config"


def linking_result_topic(station: str) -> str:
    """Topic the hub answers a station on."""
    return f"{PREFIX}/linking/{station}/result"


def discovery_payload(version: str, reader_id: str | None = None) -> dict[str, Any]:
    """Device-based discovery for every hub entity.

    ``reader_id`` (the UR4's MAC, else host:port) is added to the device
    identifiers; ``pantry_hub`` stays, so HA keeps one device either way.
    """

    def sensor(
        key: str, name: str, template: str, icon: str, **extra: Any
    ) -> dict[str, Any]:
        return {
            "p": "sensor",
            "name": name,
            "unique_id": f"pantry_hub_{key}",
            "default_entity_id": f"sensor.pantry_hub_{key}",
            "state_topic": STATE_TOPIC,
            "value_template": template,
            "icon": icon,
            **extra,
        }

    return {
        "device": {
            "identifiers": [
                "pantry_hub",
                *([f"chainway_{reader_id}"] if reader_id else []),
            ],
            "name": "Pantry Hub",
            "manufacturer": "thermitehash",
            "model": "Pantry Hub",
            "sw_version": version,
        },
        "origin": {
            "name": "Pantry Hub",
            "sw_version": version,
            "support_url": "https://github.com/thermitehash/ha-apps",
        },
        "availability_topic": AVAILABILITY_TOPIC,
        "components": {
            "reader_online": {
                "p": "binary_sensor",
                "name": "Reader online",
                "unique_id": "pantry_hub_reader_online",
                "default_entity_id": "binary_sensor.pantry_hub_reader_online",
                "state_topic": STATE_TOPIC,
                "value_template": "{{ 'ON' if value_json.reader_online else 'OFF' }}",
                "device_class": "connectivity",
                "entity_category": "diagnostic",
                "json_attributes_topic": STATE_TOPIC,
                "json_attributes_template": "{{ value_json.reader | tojson }}",
            },
            "present_tags": sensor(
                "present_tags",
                "Present tags",
                "{{ value_json.present_tags }}",
                "mdi:tag-multiple",
                state_class="measurement",
                unit_of_measurement="tags",
            ),
            "unlinked_tags": sensor(
                "unlinked_tags",
                "Unlinked tags",
                "{{ value_json.unlinked_tags }}",
                "mdi:tag-off",
                state_class="measurement",
                unit_of_measurement="tags",
            ),
            "grocy_queue": sensor(
                "grocy_queue",
                "Grocy queue",
                "{{ value_json.grocy_queue }}",
                "mdi:tray-full",
                state_class="measurement",
                unit_of_measurement="ops",
                entity_category="diagnostic",
                json_attributes_topic=STATE_TOPIC,
                json_attributes_template="{{ value_json.grocy | tojson }}",
            ),
            "last_link": {
                "p": "sensor",
                "name": "Last link",
                "unique_id": "pantry_hub_last_link",
                "default_entity_id": "sensor.pantry_hub_last_link",
                "state_topic": LAST_LINK_TOPIC,
                "value_template": "{{ value_json.product }}",
                "json_attributes_topic": LAST_LINK_TOPIC,
                "icon": "mdi:link-variant",
            },
            "resync_differences": {
                "p": "sensor",
                "name": "Resync differences",
                "unique_id": "pantry_hub_resync_differences",
                "default_entity_id": "sensor.pantry_hub_resync_differences",
                "state_topic": RESYNC_RESULT_TOPIC,
                "value_template": "{{ value_json.differences | count }}",
                "json_attributes_topic": RESYNC_RESULT_TOPIC,
                "icon": "mdi:scale-unbalanced",
                "entity_category": "diagnostic",
            },
            "reader_activity": sensor(
                "reader_activity",
                "Reader activity",
                "{{ value_json.activity }}",
                "mdi:radar",
                device_class="enum",
                options=["idle", "continuous", "sweep"],
                json_attributes_topic=STATE_TOPIC,
                json_attributes_template="{{ value_json.reading | tojson }}",
            ),
            "last_sweep": sensor(
                "last_sweep",
                "Last sweep",
                "{{ value_json.last_sweep or None }}",
                "mdi:timer-check-outline",
                device_class="timestamp",
                entity_category="diagnostic",
            ),
            "sweep_now": {
                "p": "button",
                "name": "Sweep now",
                "unique_id": "pantry_hub_sweep_now",
                "default_entity_id": "button.pantry_hub_sweep_now",
                "command_topic": SWEEP_COMMAND_TOPIC,
                "payload_press": "PRESS",
                "icon": "mdi:radar",
            },
            "item": {
                "p": "event",
                "name": "Item",
                "unique_id": "pantry_hub_item",
                "default_entity_id": "event.pantry_hub_item",
                "state_topic": EVENTS_TOPIC,
                "event_types": ["arrived", "departed"],
                "icon": "mdi:package-variant",
            },
            "resync": {
                "p": "button",
                "name": "Resync",
                "unique_id": "pantry_hub_resync",
                "default_entity_id": "button.pantry_hub_resync",
                "command_topic": RESYNC_COMMAND_TOPIC,
                "payload_press": "PRESS",
                "icon": "mdi:sync",
                "entity_category": "diagnostic",
            },
        },
    }

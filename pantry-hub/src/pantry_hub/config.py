"""Options loading and validation.

The Supervisor writes the user's options to ``/data/options.json``. For local
development (macOS, no Supervisor) the same file can live anywhere: set
``PANTRY_HUB_OPTIONS``. MQTT credentials come from the Supervisor's service
API, or from ``PANTRY_HUB_MQTT_*`` environment variables outside Home
Assistant.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import ipaddress
import json
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit

import aiohttp

#: Default UDP port the hub binds inside the container (mapped in config.yaml).
DEFAULT_UDP_PORT = 8888
#: Port the ingress web UI listens on (config.yaml ``ingress_port``).
DEFAULT_WEB_PORT = 8099
#: FCC limit context: 8.3 dBi antennas, 36 dBm EIRP → 27.7 dBm conducted.
MAX_SAFE_READER_POWER_DBM = 27
LOG_LEVELS = ("debug", "info", "warning", "error")
READING_MODES = ("door", "continuous")
READER_CONNECTIONS = ("per_window", "persistent")
DOOR_ENTITY_RE = re.compile(r"^binary_sensor\.[a-z0-9_]+$")
SUPERVISOR_MQTT_URL = "http://supervisor/services/mqtt"


class ConfigError(Exception):
    """The options are invalid; the message says which and why."""


@dataclass(frozen=True, slots=True)
class Options:
    """Validated app options."""

    reader_host: str
    grocy_url: str
    grocy_api_key: str = field(repr=False)
    grocy_location_id: int
    allowed_reader_ip: str = ""
    reader_tcp_probe: bool = False
    reader_offline_after_s: int = 120
    grocy_ui_url: str = ""
    arrive_reads: int = 3
    arrive_window_s: float = 10.0
    depart_after_s: float = 900.0
    depart_requires_door_closed: bool = False
    #: "door": command-mode TCP reading driven by the door entity (default);
    #: "continuous": the reader's auto mode pushing UDP (v0.1/0.2 behaviour).
    reading_mode: str = "door"
    door_entity: str = ""
    open_continuous_s: float = 600.0
    open_sweep_interval_s: float = 300.0
    sweep_s: float = 45.0
    closed_sweep_interval_s: float = 3600.0
    depart_after_missed_sweeps: int = 2
    reader_tcp_port: int = 8888
    #: per_window: connect for each window (default until hardware validation
    #: §E.2); persistent: one connection with keepalive + auto-reconnect.
    reader_connection: str = "per_window"
    max_reader_power_dbm: int = 27
    log_level: str = "info"
    udp_port: int = DEFAULT_UDP_PORT
    web_port: int = DEFAULT_WEB_PORT

    @property
    def reader_source_ip(self) -> str | None:
        """IP that UDP datagrams must come from, or None to accept any."""
        if self.allowed_reader_ip:
            return self.allowed_reader_ip
        try:
            return str(ipaddress.ip_address(self.reader_host))
        except ValueError:
            return None

    @property
    def grocy_browser_url(self) -> str:
        """Base URL for links in the web UI (what the user's browser can open)."""
        return (self.grocy_ui_url or self.grocy_url).rstrip("/")


def _int(raw: Mapping[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{key} must be a whole number, got {value!r}")
    if int(value) != value:
        raise ConfigError(f"{key} must be a whole number, got {value!r}")
    if not lo <= value <= hi:
        raise ConfigError(f"{key} must be between {lo} and {hi}, got {value}")
    return int(value)


def _bool(raw: Mapping[str, Any], key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{key} must be true or false, got {value!r}")
    return value


def _str(raw: Mapping[str, Any], key: str, default: str = "") -> str:
    value = raw.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be text, got {value!r}")
    return value.strip()


def _check_url(key: str, value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError(
            f"{key} must be an http:// or https:// URL with a host, got {value!r}. "
            "For the Grocy app use http://a0d7b954-grocy (see DOCS.md)."
        )
    return value.rstrip("/")


def parse_options(
    raw: Mapping[str, Any], env: Mapping[str, str] | None = None
) -> Options:
    """Validate a decoded options.json; raise :class:`ConfigError` if bad."""
    env = os.environ if env is None else env
    reader_host = _str(raw, "reader_host")
    if not reader_host:
        raise ConfigError("reader_host is required: the UR4's IP address")
    allowed = _str(raw, "allowed_reader_ip")
    if allowed:
        try:
            allowed = str(ipaddress.ip_address(allowed))
        except ValueError as err:
            raise ConfigError(
                f"allowed_reader_ip must be an IP address, got {allowed!r}"
            ) from err
    grocy_url = _check_url("grocy_url", _str(raw, "grocy_url"))
    api_key = _str(raw, "grocy_api_key")
    if not api_key:
        raise ConfigError(
            "grocy_api_key is required: create one in Grocy under "
            "Settings (wrench) → Manage API keys"
        )
    grocy_ui_url = _str(raw, "grocy_ui_url")
    if grocy_ui_url:
        grocy_ui_url = _check_url("grocy_ui_url", grocy_ui_url)
    arrive_window = _int(raw, "arrive_window_s", 10, 1, 600)
    depart_after = _int(raw, "depart_after_s", 900, 30, 86400)
    if depart_after <= arrive_window:
        raise ConfigError(
            f"depart_after_s ({depart_after}) must be longer than "
            f"arrive_window_s ({arrive_window})"
        )
    requires_door = _bool(raw, "depart_requires_door_closed", False)
    reading_mode = _str(raw, "reading_mode", "door").lower()
    if reading_mode not in READING_MODES:
        raise ConfigError(f"reading_mode must be one of {', '.join(READING_MODES)}")
    door_entity = _str(raw, "door_entity")
    if door_entity and not DOOR_ENTITY_RE.fullmatch(door_entity):
        raise ConfigError(
            f"door_entity must be a binary_sensor entity id, got {door_entity!r} "
            "(e.g. binary_sensor.pantry_door)"
        )
    if reading_mode == "door" and not door_entity:
        raise ConfigError(
            "door_entity is required when reading_mode is door: set the pantry "
            "door contact (e.g. binary_sensor.pantry_door), or use reading_mode: "
            "continuous"
        )
    if requires_door and not door_entity:
        raise ConfigError("depart_requires_door_closed is on but door_entity is empty")
    if "door_entity_topic" in raw and raw.get("door_entity_topic"):
        raise ConfigError(
            "door_entity_topic (MQTT) was replaced by door_entity (a Home "
            "Assistant binary_sensor) in 0.3.0"
        )
    open_continuous = _int(raw, "open_continuous_s", 600, 0, 86400)
    open_interval = _int(raw, "open_sweep_interval_s", 300, 30, 86400)
    sweep = _int(raw, "sweep_s", 45, 5, 3600)
    closed_interval = _int(raw, "closed_sweep_interval_s", 3600, 60, 86400)
    if sweep >= min(open_interval, closed_interval):
        raise ConfigError(
            f"sweep_s ({sweep}) must be shorter than open_sweep_interval_s "
            f"({open_interval}) and closed_sweep_interval_s ({closed_interval})"
        )
    missed = _int(raw, "depart_after_missed_sweeps", 2, 1, 20)
    connection = _str(raw, "reader_connection", "per_window").lower()
    if connection not in READER_CONNECTIONS:
        raise ConfigError(
            f"reader_connection must be one of {', '.join(READER_CONNECTIONS)}"
        )
    power = _int(raw, "max_reader_power_dbm", MAX_SAFE_READER_POWER_DBM, 0, 30)
    if power > MAX_SAFE_READER_POWER_DBM:
        raise ConfigError(
            f"max_reader_power_dbm {power} is above {MAX_SAFE_READER_POWER_DBM}: "
            "with 8.3 dBi antennas that exceeds the FCC 36 dBm EIRP limit"
        )
    log_level = _str(raw, "log_level", "info").lower()
    if log_level not in LOG_LEVELS:
        raise ConfigError(f"log_level must be one of {', '.join(LOG_LEVELS)}")
    return Options(
        reader_host=reader_host,
        allowed_reader_ip=allowed,
        reader_tcp_probe=_bool(raw, "reader_tcp_probe", False),
        reader_offline_after_s=_int(raw, "reader_offline_after_s", 120, 10, 3600),
        grocy_url=grocy_url,
        grocy_api_key=api_key,
        grocy_location_id=_int(raw, "grocy_location_id", 1, 1, 2**31 - 1),
        grocy_ui_url=grocy_ui_url,
        arrive_reads=_int(raw, "arrive_reads", 3, 1, 100),
        arrive_window_s=float(arrive_window),
        depart_after_s=float(depart_after),
        depart_requires_door_closed=requires_door,
        reading_mode=reading_mode,
        door_entity=door_entity,
        open_continuous_s=float(open_continuous),
        open_sweep_interval_s=float(open_interval),
        sweep_s=float(sweep),
        closed_sweep_interval_s=float(closed_interval),
        depart_after_missed_sweeps=missed,
        reader_tcp_port=_int(raw, "reader_tcp_port", 8888, 1, 65535),
        reader_connection=connection,
        max_reader_power_dbm=power,
        log_level=log_level,
        udp_port=int(env.get("PANTRY_HUB_UDP_PORT", DEFAULT_UDP_PORT)),
        web_port=int(env.get("PANTRY_HUB_WEB_PORT", DEFAULT_WEB_PORT)),
    )


def load_options(path: Path) -> Options:
    """Read and validate options from ``path``."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        raise ConfigError(f"options file not found: {path}") from err
    except json.JSONDecodeError as err:
        raise ConfigError(f"options file {path} is not valid JSON: {err}") from err
    if not isinstance(raw, dict):
        raise ConfigError(f"options file {path} must hold a JSON object")
    return parse_options(raw)


@dataclass(frozen=True, slots=True)
class MqttSettings:
    """Broker connection details."""

    host: str
    port: int = 1883
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    ssl: bool = False


def mqtt_from_env(env: Mapping[str, str]) -> MqttSettings | None:
    """Broker settings from ``PANTRY_HUB_MQTT_*`` (local development)."""
    host = env.get("PANTRY_HUB_MQTT_HOST")
    if not host:
        return None
    return MqttSettings(
        host=host,
        port=int(env.get("PANTRY_HUB_MQTT_PORT", "1883")),
        username=env.get("PANTRY_HUB_MQTT_USERNAME") or None,
        password=env.get("PANTRY_HUB_MQTT_PASSWORD") or None,
    )


async def mqtt_from_supervisor(
    token: str, url: str = SUPERVISOR_MQTT_URL, timeout: float = 10.0
) -> MqttSettings:
    """Ask the Supervisor for the broker it provides (``services: mqtt:need``)."""
    try:
        async with (
            aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as session,
            session.get(url, headers={"Authorization": f"Bearer {token}"}) as resp,
        ):
            if resp.status != 200:
                raise ConfigError(
                    f"Supervisor returned HTTP {resp.status} for the MQTT service. "
                    "Install the Mosquitto broker app and start it."
                )
            body = await resp.json()
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ConfigError(f"cannot reach the Supervisor MQTT service: {err}") from err
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict) or not data.get("host"):
        raise ConfigError("Supervisor MQTT service returned no broker host")
    return MqttSettings(
        host=str(data["host"]),
        port=int(data.get("port", 1883)),
        username=data.get("username") or None,
        password=data.get("password") or None,
        ssl=bool(data.get("ssl", False)),
    )

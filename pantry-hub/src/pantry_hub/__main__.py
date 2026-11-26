"""Entry point: ``python -m pantry_hub``.

Environment (all optional; defaults are for the Home Assistant app):

* ``PANTRY_HUB_OPTIONS`` — options file (default ``/data/options.json``)
* ``PANTRY_HUB_DATA`` — data directory for the SQLite DB (default ``/data``)
* ``PANTRY_HUB_MQTT_HOST`` / ``_PORT`` / ``_USERNAME`` / ``_PASSWORD`` — broker
  outside Home Assistant (otherwise asked from the Supervisor)
* ``PANTRY_HUB_WEB_ALLOW`` — comma-separated peers allowed to open the web UI
  (default: the ingress proxy only; ``*`` for local development)
* ``PANTRY_HUB_GROCY_WAIT`` — seconds to wait for Grocy at start (default 120)
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import contextlib
from importlib.metadata import PackageNotFoundError, version as pkg_version
import logging
import os
from pathlib import Path
import signal
import sys

from .config import (
    ConfigError,
    MqttSettings,
    Options,
    load_options,
    mqtt_from_env,
    mqtt_from_supervisor,
)
from .grocy import (
    GrocyAuthError,
    GrocyClient,
    GrocyError,
    GrocyRequestError,
    GrocyUnavailable,
)
from .hub import Hub
from .store import Store
from .web import WebUI

_LOGGER = logging.getLogger("pantry_hub")


def app_version() -> str:
    """Installed package version (single-sourced from config.yaml)."""
    try:
        return pkg_version("pantry-hub")
    except PackageNotFoundError:  # pragma: no cover - source checkout without install
        return os.environ.get("BUILD_VERSION", "dev")


def setup_logging(level: str) -> None:
    """Log to stdout in the Supervisor's format."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s (%(name)s) %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    if level != "debug":
        logging.getLogger("aiochainway").setLevel(logging.INFO)


async def check_grocy(client: GrocyClient, options: Options, wait: float) -> str:
    """Validate Grocy URL, API key and location; return a summary line.

    Waits up to ``wait`` seconds for Grocy to come up (it may start after us);
    a wrong key, wrong URL or missing location fails at once.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait
    delay = 2.0
    while True:
        try:
            info = await client.system_info()
            break
        except GrocyAuthError as err:
            raise ConfigError(
                f"{err}. Check grocy_api_key (Grocy → Settings → Manage API keys)"
            ) from err
        except GrocyRequestError as err:
            raise ConfigError(
                f"{options.grocy_url} answered {err} for /api/system/info; "
                "grocy_url must be the Grocy base URL"
            ) from err
        except GrocyUnavailable as err:
            if loop.time() >= deadline:
                raise ConfigError(
                    f"Grocy is not reachable at {options.grocy_url}: {err}. "
                    "For the Grocy app, enable its port under Network and use "
                    "http://a0d7b954-grocy (see the app documentation)"
                ) from err
            _LOGGER.warning("Waiting for Grocy (%s)", err)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15.0)
        except GrocyError as err:
            raise ConfigError(str(err)) from err
    try:
        location = await client.location_name(options.grocy_location_id)
    except GrocyRequestError as err:
        raise ConfigError(
            f"grocy_location_id {options.grocy_location_id} does not exist in Grocy "
            "(Grocy → Manage master data → Locations)"
        ) from err
    except GrocyError as err:
        raise ConfigError(f"cannot read Grocy locations: {err}") from err
    grocy_version = info.get("grocy_version", {})
    ver = grocy_version.get("Version", "?") if isinstance(grocy_version, dict) else "?"
    return f"Grocy {ver} OK; pantry location is {location!r} (id {options.grocy_location_id})"


async def resolve_mqtt(env: Mapping[str, str]) -> MqttSettings:
    """Broker from env (dev) or the Supervisor (app)."""
    settings = mqtt_from_env(env)
    if settings is not None:
        return settings
    token = env.get("SUPERVISOR_TOKEN")
    if not token:
        raise ConfigError(
            "no MQTT broker: not running under the Supervisor and "
            "PANTRY_HUB_MQTT_HOST is not set"
        )
    return await mqtt_from_supervisor(token)


async def run(
    options: Options,
    data_dir: Path,
    env: Mapping[str, str],
    stop: asyncio.Event | None = None,
) -> int:
    """Validate, start, wait for SIGTERM/SIGINT (or ``stop``), shut down."""
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)
    version = app_version()
    _LOGGER.info("Pantry Hub %s starting", version)
    grocy = GrocyClient(options.grocy_url, options.grocy_api_key)
    try:
        summary = await check_grocy(
            grocy, options, float(env.get("PANTRY_HUB_GROCY_WAIT", "120"))
        )
        _LOGGER.info(summary)
        mqtt = await resolve_mqtt(env)
    except ConfigError as err:
        _LOGGER.error("Refusing to start: %s", err)
        await grocy.close()
        return 1
    await asyncio.to_thread(data_dir.mkdir, parents=True, exist_ok=True)
    store = await Store.open(data_dir / "pantry_hub.db")
    hub = Hub(options, store, grocy, mqtt_settings=mqtt, version=version)
    allow_env = env.get("PANTRY_HUB_WEB_ALLOW")
    web = WebUI(
        hub,
        allowed_peers=[p.strip() for p in allow_env.split(",")] if allow_env else None,
    )
    try:
        await hub.start()
        await web.start("0.0.0.0", options.web_port)
        _LOGGER.info("Pantry Hub running")
        await stop.wait()
    finally:
        await web.stop()
        await hub.stop()
        await store.close()
        await grocy.close()
        _LOGGER.info("Pantry Hub stopped")
    return 0


def main() -> int:
    """Console entry point."""
    env = os.environ
    options_path = Path(env.get("PANTRY_HUB_OPTIONS", "/data/options.json"))
    data_dir = Path(env.get("PANTRY_HUB_DATA", "/data"))
    setup_logging("info")
    try:
        options = load_options(options_path)
    except ConfigError as err:
        _LOGGER.error("Refusing to start: %s", err)
        return 1
    setup_logging(options.log_level)
    return asyncio.run(run(options, data_dir, env))


if __name__ == "__main__":
    sys.exit(main())

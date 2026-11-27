"""MQTT connection with reconnect, availability (LWT) and an outbox."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
import contextlib
from dataclasses import dataclass
import logging
import random
import ssl

import aiomqtt

from .config import MqttSettings
from .discovery import AVAILABILITY_TOPIC

_LOGGER = logging.getLogger(__name__)

MessageHandler = Callable[[str, bytes], Awaitable[None]]
ConnectHandler = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _Pending:
    topic: str
    payload: bytes
    retain: bool
    qos: int


class MqttLink:
    """Keep one MQTT session up; deliver messages; queue publishes while down."""

    def __init__(
        self,
        settings: MqttSettings,
        *,
        subscriptions: list[str],
        on_message: MessageHandler,
        on_connect: ConnectHandler,
        client_id: str = "pantry_hub",
        min_backoff: float = 1.0,
        max_backoff: float = 60.0,
        outbox_size: int = 1000,
        op_timeout: float = 5.0,
    ) -> None:
        """Create; call :meth:`start`."""
        self.settings = settings
        self.subscriptions = subscriptions
        self.on_message = on_message
        self.on_connect = on_connect
        self.client_id = client_id
        self.min_backoff = min_backoff
        self.max_backoff = max_backoff
        self.op_timeout = op_timeout
        self._outbox: deque[_Pending] = deque(maxlen=outbox_size)
        self.outbox_dropped = 0
        self.connects = 0
        self._client: aiomqtt.Client | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._connected = asyncio.Event()
        self._rng = random.Random()

    @property
    def connected(self) -> bool:
        """Whether a session is up."""
        return self._client is not None and self._connected.is_set()

    async def wait_connected(self, timeout: float) -> bool:
        """Wait until connected (tests, startup)."""
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
        except TimeoutError:
            return False
        return True

    def start(self) -> None:
        """Start the connection loop."""
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="pantry_hub.mqtt")

    async def stop(self) -> None:
        """Publish ``offline`` and disconnect."""
        self._stopping.set()
        client = self._client
        if client is not None and self._connected.is_set():
            try:
                await asyncio.wait_for(
                    client.publish(AVAILABILITY_TOPIC, b"offline", qos=1, retain=True),
                    5,
                )
            except (aiomqtt.MqttError, TimeoutError) as err:
                _LOGGER.warning("Could not publish offline availability: %s", err)
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def publish(
        self,
        topic: str,
        payload: str | bytes,
        *,
        retain: bool = False,
        qos: int = 0,
        queue: bool = True,
    ) -> bool:
        """Publish now if connected; otherwise queue (if ``queue``) for reconnect.

        Returns whether it was sent now.
        """
        data = payload.encode() if isinstance(payload, str) else payload
        client = self._client
        if client is not None and self._connected.is_set():
            try:
                await client.publish(topic, data, qos=qos, retain=retain)
            except aiomqtt.MqttError as err:
                _LOGGER.warning("MQTT publish to %s failed: %s", topic, err)
            else:
                return True
        if queue:
            if len(self._outbox) == self._outbox.maxlen:
                self.outbox_dropped += 1
                _LOGGER.warning(
                    "MQTT outbox full; dropped the oldest message (%d dropped so far)",
                    self.outbox_dropped,
                )
            self._outbox.append(_Pending(topic, data, retain, qos))
        return False

    def _tls(self) -> aiomqtt.TLSParameters | None:
        if not self.settings.ssl:
            return None
        return aiomqtt.TLSParameters(cert_reqs=ssl.CERT_REQUIRED)

    async def _run(self) -> None:
        failures = 0
        while not self._stopping.is_set():
            try:
                async with aiomqtt.Client(
                    hostname=self.settings.host,
                    port=self.settings.port,
                    username=self.settings.username,
                    password=self.settings.password,
                    identifier=self.client_id,
                    keepalive=30,
                    # Bounds every operation, e.g. a QoS 1 publish whose PUBACK
                    # never comes because the broker went away mid-publish.
                    timeout=self.op_timeout,
                    tls_params=self._tls(),
                    will=aiomqtt.Will(
                        AVAILABILITY_TOPIC, b"offline", qos=1, retain=True
                    ),
                ) as client:
                    self._client = client
                    for topic in self.subscriptions:
                        await client.subscribe(topic, qos=1)
                    await client.publish(
                        AVAILABILITY_TOPIC, b"online", qos=1, retain=True
                    )
                    self._connected.set()
                    self.connects += 1
                    failures = 0
                    _LOGGER.info(
                        "MQTT connected to %s:%d",
                        self.settings.host,
                        self.settings.port,
                    )
                    await self._safe_on_connect()
                    await self._flush(client)
                    async for message in client.messages:
                        payload = message.payload
                        data = (
                            payload
                            if isinstance(payload, bytes)
                            else str(payload or "").encode()
                        )
                        try:
                            await self.on_message(str(message.topic), data)
                        except Exception:
                            _LOGGER.exception(
                                "Error handling MQTT message on %s", message.topic
                            )
            except aiomqtt.MqttError as err:
                if self._stopping.is_set():
                    break
                failures += 1
                _LOGGER.warning("MQTT connection lost or refused: %s", err)
            finally:
                self._connected.clear()
                self._client = None
            delay = min(self.max_backoff, self.min_backoff * 2 ** min(failures, 10))
            delay *= self._rng.uniform(0.5, 1.0)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), delay)

    async def _safe_on_connect(self) -> None:
        try:
            await self.on_connect()
        except aiomqtt.MqttError:
            raise
        except Exception:
            _LOGGER.exception("Error in MQTT on-connect handler")

    async def _flush(self, client: aiomqtt.Client) -> None:
        while self._outbox:
            item = self._outbox[0]
            await client.publish(
                item.topic, item.payload, qos=item.qos, retain=item.retain
            )
            self._outbox.popleft()

"""Integration rig: real Hub + aiochainway fake UR4 frames + fake Grocy + broker."""

from __future__ import annotations

import asyncio
from collections import defaultdict
import contextlib
import json
from pathlib import Path
import socket
from typing import Any

from aiochainway.testing import tag_frames, tag_record
import aiomqtt

from pantry_hub.config import MqttSettings, Options
from pantry_hub.grocy import GrocyClient
from pantry_hub.hub import Hub
from pantry_hub.store import Store

from .conftest import BrokerHandle, FakeClock, eventually, make_options
from .fake_grocy import API_KEY, FakeGrocy


class Observer:
    """MQTT client that records everything it sees."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.messages: dict[str, list[bytes]] = defaultdict(list)
        self._task: asyncio.Task[None] | None = None
        self._client: aiomqtt.Client | None = None
        self._ready = asyncio.Event()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())
        await asyncio.wait_for(self._ready.wait(), 5)

    async def _run(self) -> None:
        while True:
            try:
                async with aiomqtt.Client(
                    "127.0.0.1", self.port, identifier="observer"
                ) as c:
                    self._client = c
                    await c.subscribe("#")
                    self._ready.set()
                    async for m in c.messages:
                        payload = (
                            m.payload
                            if isinstance(m.payload, bytes)
                            else str(m.payload).encode()
                        )
                        self.messages[str(m.topic)].append(payload)
            except aiomqtt.MqttError:
                self._client = None
                await asyncio.sleep(0.2)

    async def publish(self, topic: str, payload: Any, retain: bool = False) -> None:
        assert self._client is not None
        data = payload if isinstance(payload, bytes | str) else json.dumps(payload)
        await self._client.publish(topic, data, qos=1, retain=retain)

    def json(self, topic: str) -> list[Any]:
        return [json.loads(p) for p in self.messages.get(topic, [])]

    def last(self, topic: str) -> Any:
        items = self.json(topic)
        return items[-1] if items else None

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task


class Rig:
    def __init__(
        self,
        tmp: Path,
        grocy: FakeGrocy,
        broker: BrokerHandle | None,
        clock: FakeClock,
        **opts: Any,
    ) -> None:
        self.tmp = tmp
        self.grocy = grocy
        self.broker = broker
        self.clock = clock
        opts.setdefault("grocy_url", grocy.url)
        self.options: Options = make_options(**opts)
        self.hub: Hub | None = None
        self.store: Store | None = None
        self.client: GrocyClient | None = None
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    @property
    def h(self) -> Hub:
        assert self.hub is not None
        return self.hub

    async def start(self, **hub_kw: Any) -> Hub:
        self.store = await Store.open(self.tmp / "hub.db")
        self.client = GrocyClient(self.grocy.url, API_KEY, timeout=0.5)
        mqtt = MqttSettings("127.0.0.1", self.broker.port) if self.broker else None
        hub_kw.setdefault("tag_timeout", 1.0)
        hub_kw.setdefault("rssi_window", 0.1)
        self.hub = Hub(
            self.options,
            self.store,
            self.client,
            mqtt_settings=mqtt,
            version="0.1.0-test",
            monotonic=self.clock,
            wall=self.clock,
            tick_s=0.01,
            bind_host="127.0.0.1",
            **hub_kw,
        )
        self.hub.worker.backoff.base = 0.01
        self.hub.worker.backoff.cap = 0.05
        self.hub.worker.verify_after = 1.5  # > FakeGrocy.hang_s in fault tests
        if self.hub.mqtt is not None:
            self.hub.mqtt.min_backoff = 0.05
            self.hub.mqtt.max_backoff = 0.2
        await self.hub.start()
        if self.hub.mqtt is not None:
            assert await self.hub.mqtt.wait_connected(5)
        return self.hub

    async def stop(self) -> None:
        if self.hub is not None:
            await self.hub.stop()
            self.hub = None
        if self.store is not None:
            await self.store.close()
            self.store = None
        if self.client is not None:
            await self.client.close()
            self.client = None

    def close(self) -> None:
        self.sock.close()

    @property
    def udp_addr(self) -> tuple[str, int]:
        assert self.h.listener is not None
        addr = self.h.listener.local_address
        assert addr is not None
        return addr

    def send(self, *epcs: str, rssi: float = -55.0) -> None:
        self.sock.sendto(
            tag_frames(*(tag_record(e, rssi_dbm=rssi) for e in epcs)), self.udp_addr
        )

    def send_raw(self, data: bytes) -> None:
        self.sock.sendto(data, self.udp_addr)

    async def read_until_state(
        self, epc: str, state: str, timeout: float = 3.0
    ) -> None:
        await eventually(lambda: self.h.machine.state(epc).value == state, timeout)

    async def arrive(self, *epcs: str) -> None:
        """Send enough reads for every EPC to arrive."""
        for _ in range(self.options.arrive_reads):
            self.send(*epcs)
            await asyncio.sleep(0.02)
        for epc in epcs:
            await self.read_until_state(epc, "present")

    async def idle(self) -> None:
        """Let queued events and the worker settle."""
        # Regression: "queue empty" is not "handled": an event taken off the
        # queue may still be committing, so wait for task_done() on all of them.
        await asyncio.wait_for(self.h.drain_events(), 5)
        store = self.store
        assert store is not None
        await eventually(self._queue_empty)

    async def _queue_empty(self) -> bool:
        assert self.store is not None
        counts = await self.store.counts()
        return counts["pending"] == 0 and counts["verifying"] == 0

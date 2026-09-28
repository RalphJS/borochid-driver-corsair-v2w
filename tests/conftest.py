from __future__ import annotations

import asyncio
import copy
from pathlib import Path

import pytest

from borochid.common.manifest import Manifest
from borochid.common.models import Bus, DeviceIdentity
from borochid.service.channels import Channel, PairedChannel
from borochid.service.settings import MemoryStore

from borochid_corsair_v2w.driver import HeadsetDriver
from borochid_corsair_v2w.receiver import ReceiverDriver

WIRELESS_PID, WIRED_PID, STANDBY_PID = 0x0A3E, 0x0A3D, 0x0A46
HEADSET_ID = "6BBD758E8AB985D6"

TIMINGS = {
    "keepalive_s": 0.05,
    "probe_s": 0.05,
    "probe_max_s": 0.05,
    "reply_timeout_s": 0.05,
    "first_contact_s": 0.3,
    "write_gap_s": 0,
}

# The headset: on its cable, and behind its dongle (announced by the dongle's driver).
MANIFEST = {
    "id": "test.headset",
    "version": "1.0.0",
    "match": [
        {"bus": "usb", "vid": "0x1b1c", "pid": "0x0a3e", "paired": True},
        {"bus": "usb", "vid": "0x1b1c", "pid": "0x0a3d"},
    ],
    "channel": {"type": "hid", "interface": [4, 3]},
    "driver": {"type": "corsair-v2w"},
    "v2w": {
        "links": {"wireless": ["0x0a3e"], "wired": ["0x0a3d"]},
        **TIMINGS,
        "features": {
            "lighting": {
                "zones": [
                    {"id": "logo", "kind": "color"},
                    {"id": "battery", "kind": "battery"},
                    {"id": "mic", "kind": "mic", "default": "#00ff00", "muted_default": "#ff0000"},
                ]
            },
            "battery": {"poll_s": 0.05, "low_percent": 15},
            "sidetone": {},
            "mic_button": {},
        },
    },
}

# The dongle itself.
RECEIVER_MANIFEST = {
    "id": "test.receiver",
    "version": "1.0.0",
    "category": "receiver",
    "match": [
        {"bus": "usb", "vid": "0x1b1c", "pid": "0x0a3e", "paired": False},
        {"bus": "usb", "vid": "0x1b1c", "pid": "0x0a46", "channel": None},
    ],
    "channel": {"type": "hid", "interface": [4, 3]},
    "driver": {"type": "corsair-v2w-receiver"},
    "v2w": {"links": {"wireless": ["0x0a3e"], "standby": ["0x0a46"]}, **TIMINGS},
}


class FakeHeadset(Channel):
    """A V2W dongle + headset pair, replying in the format captured from a
    Virtuoso SE: ``01 <source> <command> <status> <lo> <hi>``. With the
    wired PID it is the headset on its cable, which answers on the dongle's
    endpoint (source 00)."""

    RECEIVER_PID, HEADSET_PID = 0x0A3E, 0x0A3D

    def __init__(self, pid: int = WIRELESS_PID):
        super().__init__(DeviceIdentity(Bus.USB, "usb:1-2.4", vid=0x1B1C, pid=pid, serial="T1"), {"type": "hid"})
        self.powered = True
        self.mode = {0x08: 0x01, 0x09: 0x01}  # hardware mode on both ends
        self.battery_raw = 850
        self.charging = 2
        # After power-on / re-enumeration the radio link takes a while: every
        # headset reply waits until it is up (captured: ~600 ms).
        self.first_contact_delay = 0.0
        self._link_up_at: float | None = None
        # USB jitter on the dongle's own replies.
        self.dongle_delay = 0.0
        # Opcodes this model rejects (captured: a Virtuoso SE answers the
        # Void-family sidetone frames with status 05).
        self.rejected_ops: set[int] = set()
        # Data resource 0x05 (captured from a Virtuoso SE, cable and radio alike).
        self.headset_id = bytes.fromhex(HEADSET_ID)
        self.handles: dict[int, int] = {}  # open handle -> resource
        self.frames: list[tuple[int, int, bytes]] = []
        self.painted: list[list[tuple[int, int, int]]] = []

    async def open(self): ...

    async def close(self): ...

    def count(self, endpoint: int, command: int, op: int) -> int:
        return sum(1 for ep, cmd, p in self.frames if (ep, cmd) == (endpoint, command) and p[:1] == bytes([op]))

    async def write(self, data: bytes) -> None:
        assert len(data) == 65 and data[:2] == b"\x00\x02"
        ep, cmd, payload = data[2], data[3], data[4:]
        self.frames.append((ep, cmd, payload.rstrip(b"\0")))
        if self.ident.pid == WIRED_PID:
            assert ep == 0x08, "on the cable the headset answers on the dongle's endpoint"
            self._headset(ep, 0x00, cmd, payload)
            return
        if ep == 0x08:
            # The dongle always answers its own commands, headset or not.
            if cmd == 0x02 and payload[0] == 0x13:
                self._reply(0x00, cmd, 0, data=bytes([0x00, 0x10, 0x50]))  # firmware 0.16.80
                return
            value = {0x12: self.RECEIVER_PID}.get(payload[0], 0) if cmd == 0x02 else 0
            if cmd == 0x01 and payload[0] == 0x03:
                self.mode[ep] = payload[2]
            self._reply(0x00, cmd, value)
            return
        if self.powered:
            self._headset(ep, 0x01, cmd, payload)

    def _headset(self, ep: int, source: int, cmd: int, payload: bytes) -> None:
        if cmd == 0x01 and payload[0] == 0x03:
            self.mode[ep] = payload[2]
        if cmd in (0x05, 0x08, 0x0D):
            self._resources(source, cmd, payload)
        elif cmd == 0x02 and payload[0] == 0x0F:
            self._reply(source, cmd, self.battery_raw)
        elif cmd == 0x02 and payload[0] == 0x10:
            self._reply(source, cmd, self.charging)
        elif cmd == 0x02 and payload[0] == 0x12:
            # Same shape as a battery reply; its value (the headset's own PID,
            # 2621) is what reads as a "stale >100%" battery level.
            self._reply(source, cmd, self.HEADSET_PID)
        else:
            if cmd == 0x06:
                n = payload[1] // 3
                d = payload[5 : 5 + 3 * n]
                self.painted.append([(d[i], d[n + i], d[2 * n + i]) for i in range(n)])
            self._reply(source, cmd, 0, status=0x05 if payload[0] in self.rejected_ops else 0x00)

    def _resources(self, source: int, cmd: int, payload: bytes) -> None:
        """Open (0d <handle> <resource>), read (08 <handle>), close (05 01 <handle>)."""
        if cmd == 0x0D:
            self.handles[payload[0]] = payload[1]
        elif cmd == 0x05:
            self.handles.pop(payload[1], None)
        elif cmd == 0x08:
            if self.handles.get(payload[0]) == 0x05:
                self._reply(source, cmd, 0, data=self.headset_id)
            else:
                self._reply(source, cmd, 0, status=0x03)
            return
        self._reply(source, cmd, 0)

    def _reply(self, source: int, cmd: int, value: int, status: int = 0x00, data: bytes | None = None) -> None:
        payload = data if data is not None else bytes([value & 0xFF, value >> 8])
        report = bytes([0x01, source, cmd, status, *payload]).ljust(64, b"\0")
        loop = asyncio.get_running_loop()
        if source == 0x00:
            loop.call_later(self.dongle_delay, self._deliver, report)
            return
        if self._link_up_at is None:
            self._link_up_at = loop.time() + self.first_contact_delay
        # Replies queue until the link is up, then arrive in order.
        loop.call_at(max(loop.time(), self._link_up_at), self._deliver, report)

    def power_cycle(self, on: bool) -> None:
        self.powered = on
        if on:
            self.mode[0x09] = 0x01  # a power-cycled headset forgets software mode
            self._link_up_at = None

    def announce(self) -> None:
        """Hypothetical dongle status notice when the headset reconnects."""
        self._deliver(bytes([0x03, 0x01, 0x01, 0x36, 0x00, 0x02, 0x00]).ljust(64, b"\0"))

    def press_mic(self) -> None:
        headset = 0x08 if self.ident.pid == WIRED_PID else 0x09
        assert self.mode[headset] == 0x02, "the button is only forwarded in software mode"
        for down in (1, 0):
            self._deliver(bytes([0x03, 0x01, 0x02, down]).ljust(64, b"\0"))

    def notify_battery(self, percent: int) -> None:
        raw = percent * 10
        self._deliver(bytes([0x03, 0x01, 0x01, 0x0F, 0x00, raw & 0xFF, raw >> 8]).ljust(64, b"\0"))


class FakeAudio:
    def __init__(self):
        self.mic_muted = False
        self._listeners = []

    def on_mic_muted(self, fn):
        self._listeners.append(fn)

    async def toggle_mic_mute(self):
        self.mic_muted = not self.mic_muted
        for fn in self._listeners:
            if asyncio.iscoroutine(result := fn(self.mic_muted)):
                await result
        return self.mic_muted


class FakeHost:
    def __init__(self):
        self.audio = FakeAudio()


def _manifest(base, v2w):
    m = copy.deepcopy(base)
    m["v2w"].update(v2w)
    return Manifest.from_json(m)


class Rig:
    """The dongle's driver, and the headset's while the dongle announces it,
    wired up the way the service does: the headset gets a PairedChannel, is
    started when announced, stopped when withdrawn, and stopped before the
    dongle's driver. Its settings store is kept across pairings, as the
    service keeps them under the headset's ID."""

    def __init__(self, pid=WIRELESS_PID, settings=None, **v2w):
        self.device = FakeHeadset(pid)
        self.events: list[dict] = []
        self.store = MemoryStore(settings)
        self.host = FakeHost()
        self.identified: list[str] = []
        self.pairings = 0
        self._v2w = v2w
        self.receiver = ReceiverDriver(_manifest(RECEIVER_MANIFEST, v2w), Path("."), self.device, self.events.append,
                                       MemoryStore(), None)
        self.receiver.on_pair = self._pair
        self.receiver.on_unpair = self._unpair
        self.device.on_data = self.receiver.on_data
        self.headset: HeadsetDriver | None = None
        self._pending: set[asyncio.Task] = set()

    def _pair(self, slot, name, pid, shared):
        assert slot == "headset"
        self._unpair(slot)
        ident = DeviceIdentity(Bus.USB, f"usb:1-2.4/{slot}", vid=0x1B1C, pid=self.device.ident.pid, attrs={"receiver": "x"})
        channel = PairedChannel(ident, self.device, shared)
        driver = HeadsetDriver(_manifest(MANIFEST, self._v2w), Path("."), channel, self.events.append, self.store, self.host)
        driver.on_identify = self.identified.append
        channel.on_data = driver.on_data
        self.headset = driver
        self.pairings += 1
        self._track(driver.start())
        return channel

    def _unpair(self, slot):
        if self.headset is not None:
            driver, self.headset = self.headset, None
            self._track(driver.stop())

    def _track(self, coro):
        task = asyncio.get_running_loop().create_task(coro)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def settle(self, seconds: float = 0.0):
        await asyncio.sleep(seconds)
        while self._pending:
            await asyncio.gather(*list(self._pending))

    async def start(self):
        await self.receiver.start()
        await self.settle()

    async def stop(self):
        self._unpair("headset")
        await self.settle()
        await self.receiver.stop()


def make_cable(settings=None, **v2w):
    """The headset on its USB cable."""
    device = FakeHeadset(WIRED_PID)
    host = FakeHost()
    driver = HeadsetDriver(_manifest(MANIFEST, v2w), Path("."), device, [].append, MemoryStore(settings), host)
    device.on_data = driver.on_data
    identified: list[str] = []
    driver.on_identify = identified.append
    return driver, device, identified, host


@pytest.fixture
def run():
    def runner(coro):
        return asyncio.run(asyncio.wait_for(coro, 5))

    return runner
